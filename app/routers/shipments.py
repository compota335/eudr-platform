"""Shipment detail: attach/detach the plots that back a consignment.

Server-rendered page for one shipment: its facts, the plots currently
attached, an attach form listing the client's remaining plots (every plot
under the client's suppliers not already on this shipment), and the Due
Diligence Statements assembled from it. Assembly itself lives in
``app.routers.dds`` (``POST /dds``); this page only links to it. Mutations
follow Post/Redirect/Get so a refresh never re-submits.

Fail-loud per the house rules: an unknown shipment or plot id is a 404, a plot
belonging to a different client or one already attached is a 400, and an
attach submission is validated in FULL before anything mutates — a single bad
id means nothing is attached.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models.plot import Plot
from app.models.shipment import Shipment
from app.models.supplier import Supplier
from app.templating import templates

router = APIRouter()


# --------------------------------------------------------------------------- #
# One shipment: detail                                                          #
# --------------------------------------------------------------------------- #
@router.get("/shipments/{shipment_id}", response_class=HTMLResponse)
def shipment_detail_page(
    request: Request,
    shipment_id: int,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Render one shipment: facts, attached plots, attachable plots, DDS."""
    shipment = _get_shipment(session, shipment_id)
    attached_ids = [plot.id for plot in shipment.plots]
    query = (
        select(Plot)
        .join(Supplier)
        .where(Supplier.client_id == shipment.client_id)
        .order_by(Plot.id)
    )
    if attached_ids:
        query = query.where(Plot.id.not_in(attached_ids))
    available_plots = list(session.scalars(query))
    return templates.TemplateResponse(
        request,
        "shipment_detail.html",
        {
            "title": shipment.reference or f"Shipment {shipment.id}",
            "shipment": shipment,
            "available_plots": available_plots,
        },
    )


# --------------------------------------------------------------------------- #
# Attach / detach plots                                                         #
# --------------------------------------------------------------------------- #
@router.post("/shipments/{shipment_id}/plots")
def attach_plots(
    shipment_id: int,
    plot_ids: list[int] = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Attach plots to a shipment atomically and redirect back (303).

    Every submitted id is validated BEFORE anything mutates: an unknown plot
    is a 404 naming it, a plot of a different client is a 400, and a plot
    already attached (including a duplicate within the same submission) is a
    400. Any failure means no plot is attached — there is no partial attach.
    """
    shipment = _get_shipment(session, shipment_id)
    taken = {plot.id for plot in shipment.plots}
    to_attach: list[Plot] = []
    for plot_id in plot_ids:
        plot = session.get(Plot, plot_id)
        if plot is None:
            raise HTTPException(status_code=404, detail=f"Plot {plot_id} not found.")
        if plot.supplier.client_id != shipment.client_id:
            raise HTTPException(
                status_code=400,
                detail=f"Plot {plot_id} belongs to a different client.",
            )
        if plot.id in taken:
            raise HTTPException(
                status_code=400,
                detail=f"Plot {plot_id} is already attached to this shipment.",
            )
        taken.add(plot.id)
        to_attach.append(plot)
    shipment.plots.extend(to_attach)
    session.commit()
    return RedirectResponse(f"/shipments/{shipment.id}", status_code=303)


@router.post("/shipments/{shipment_id}/plots/{plot_id}/detach")
def detach_plot(
    shipment_id: int,
    plot_id: int,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Detach one plot from a shipment and redirect back (303).

    404 if the shipment does not exist or the plot is not attached to it. The
    plot row itself is never deleted — only the association is removed.
    """
    shipment = _get_shipment(session, shipment_id)
    attached = next((plot for plot in shipment.plots if plot.id == plot_id), None)
    if attached is None:
        raise HTTPException(
            status_code=404,
            detail=f"Plot {plot_id} is not attached to this shipment.",
        )
    shipment.plots.remove(attached)
    session.commit()
    return RedirectResponse(f"/shipments/{shipment.id}", status_code=303)


# --------------------------------------------------------------------------- #
# Helpers (fail-loud lookup)                                                    #
# --------------------------------------------------------------------------- #
def _get_shipment(session: Session, shipment_id: int) -> Shipment:
    """Return the shipment or raise a 404."""
    shipment = session.get(Shipment, shipment_id)
    if shipment is None:
        raise HTTPException(status_code=404, detail=f"Shipment {shipment_id} not found.")
    return shipment
