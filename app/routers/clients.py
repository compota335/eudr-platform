"""Client onboarding: the operator-facing CRUD for clients and their supply base.

Server-rendered pages (no HTMX): a client list with a "new client" form, and a
per-client detail page exposing the client's suppliers, shipments and Due
Diligence Statements, each with an inline creation form. Mutations follow
Post/Redirect/Get so a refresh never re-submits.

Fail-loud per the house rules: an unknown client id is a 404, a blank required
field or an unknown enum value (``side`` / ``commodity``) is a 400, and an
unparseable ``quantity_kg`` is a 400. Empty optional fields are stored as NULL
(``""`` is treated as "not provided"); nothing is fabricated.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models.client import Client
from app.models.enums import ClientSide, Commodity
from app.models.shipment import Shipment
from app.models.supplier import Supplier
from app.routers._forms import (
    clean_optional,
    parse_enum,
    parse_optional_enum,
    parse_optional_float,
    require,
)
from app.templating import templates

router = APIRouter()


# --------------------------------------------------------------------------- #
# Clients: list + create                                                       #
# --------------------------------------------------------------------------- #
@router.get("/clients", response_class=HTMLResponse)
def clients_page(
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Render the client list with a form to onboard a new client."""
    clients = list(session.scalars(select(Client).order_by(Client.id)))
    return templates.TemplateResponse(
        request,
        "clients.html",
        {
            "title": "Clients",
            "clients": clients,
            "sides": list(ClientSide),
        },
    )


@router.post("/clients")
def create_client(
    name: str = Form(...),
    side: str = Form(...),
    country: str | None = Form(default=None),
    contact_email: str | None = Form(default=None),
    eori: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Create a client and redirect to its detail page (303)."""
    client = Client(
        name=require(name, "name"),
        side=parse_enum(ClientSide, side, "side"),
        country=clean_optional(country),
        contact_email=clean_optional(contact_email),
        eori=clean_optional(eori),
        notes=clean_optional(notes),
    )
    session.add(client)
    session.commit()
    session.refresh(client)
    return RedirectResponse(f"/clients/{client.id}", status_code=303)


# --------------------------------------------------------------------------- #
# One client: detail                                                           #
# --------------------------------------------------------------------------- #
@router.get("/clients/{client_id}", response_class=HTMLResponse)
def client_detail_page(
    request: Request,
    client_id: int,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Render one client with its suppliers, shipments and statements."""
    client = _get_client(session, client_id)
    return templates.TemplateResponse(
        request,
        "client_detail.html",
        {
            "title": client.name,
            "client": client,
            "commodities": list(Commodity),
        },
    )


# --------------------------------------------------------------------------- #
# Nested create: suppliers + shipments                                         #
# --------------------------------------------------------------------------- #
@router.post("/clients/{client_id}/suppliers")
def create_supplier(
    client_id: int,
    name: str = Form(...),
    country: str | None = Form(default=None),
    commodity: str | None = Form(default=None),
    contact_email: str | None = Form(default=None),
    contact_phone: str | None = Form(default=None),
    language: str | None = Form(default=None),
    approx_volume_t: str | None = Form(default=None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Add a supplier to a client and redirect back to the detail page (303)."""
    client = _get_client(session, client_id)
    supplier = Supplier(
        client_id=client.id,
        name=require(name, "name"),
        country=clean_optional(country),
        commodity=parse_optional_enum(Commodity, commodity, "commodity"),
        contact_email=clean_optional(contact_email),
        contact_phone=clean_optional(contact_phone),
        language=clean_optional(language),
        approx_volume_t=parse_optional_float(approx_volume_t, "approx_volume_t"),
    )
    session.add(supplier)
    session.commit()
    return RedirectResponse(f"/clients/{client.id}", status_code=303)


@router.post("/clients/{client_id}/shipments")
def create_shipment(
    client_id: int,
    reference: str | None = Form(default=None),
    commodity: str | None = Form(default=None),
    cn_code: str | None = Form(default=None),
    quantity_kg: str | None = Form(default=None),
    country_of_production: str | None = Form(default=None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Create a pending shipment for a client and redirect back (303)."""
    client = _get_client(session, client_id)
    shipment = Shipment(
        client_id=client.id,
        reference=clean_optional(reference),
        commodity=parse_optional_enum(Commodity, commodity, "commodity"),
        cn_code=clean_optional(cn_code),
        quantity_kg=parse_optional_float(quantity_kg, "quantity_kg"),
        country_of_production=clean_optional(country_of_production),
    )
    session.add(shipment)
    session.commit()
    return RedirectResponse(f"/clients/{client.id}", status_code=303)


# --------------------------------------------------------------------------- #
# Helpers (fail-loud parsing / lookup)                                         #
# --------------------------------------------------------------------------- #
def _get_client(session: Session, client_id: int) -> Client:
    """Return the client or raise a 404."""
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail=f"Client {client_id} not found.")
    return client
