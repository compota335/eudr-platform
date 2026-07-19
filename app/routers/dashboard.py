"""Internal dashboard: real, at-a-glance counts of the pipeline's state.

Every number rendered here is queried live from the database; nothing is
fabricated or cached. Where a capability is not yet tracked, the page says so
honestly rather than inventing a metric:

* Scope checks are a stateless tool (the scope checker persists nothing), so
  there is no "scope checks performed" count — the checker appears only as a
  quick link.
* DDS filing to TRACES is not integrated yet, so ``dds_filed`` counts only
  statements that reached an actually-submitted status (``submitted`` or
  ``accepted``) and stays at zero until that integration exists; assembled
  statements carry an internal reference number, not a TRACES one.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import (
    DDS,
    Client,
    Plot,
    PlotCheck,
    Shipment,
    Supplier,
)
from app.models.enums import DDSStatus, RiskLevel
from app.templating import templates

router = APIRouter()


def _count(session: Session, model: type[Any]) -> int:
    """Total row count for a model (SQLAlchemy 2.0 style)."""
    return session.scalar(select(func.count()).select_from(model)) or 0


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Render the internal dashboard with live counts from the database."""
    dds_assembled = (
        session.scalar(
            select(func.count())
            .select_from(DDS)
            .where(DDS.status == DDSStatus.assembled)
        )
        or 0
    )
    dds_filed = (
        session.scalar(
            select(func.count())
            .select_from(DDS)
            .where(DDS.status.in_((DDSStatus.submitted, DDSStatus.accepted)))
        )
        or 0
    )
    plot_checks_red = (
        session.scalar(
            select(func.count())
            .select_from(PlotCheck)
            .where(PlotCheck.risk_level == RiskLevel.red)
        )
        or 0
    )

    recent = list(
        session.scalars(
            select(PlotCheck).order_by(PlotCheck.created_at.desc()).limit(5)
        )
    )
    recent_plot_checks = [
        {
            "token": check.token[:12],
            "risk_level": check.risk_level.value if check.risk_level else None,
            "area_ha": check.area_ha,
            "created_at": check.created_at,
        }
        for check in recent
    ]

    context: dict[str, Any] = {
        "title": "Dashboard",
        "clients": _count(session, Client),
        "suppliers": _count(session, Supplier),
        "plots": _count(session, Plot),
        "shipments": _count(session, Shipment),
        "dds_total": _count(session, DDS),
        "dds_assembled": dds_assembled,
        "dds_filed": dds_filed,
        "plot_checks": _count(session, PlotCheck),
        "plot_checks_red": plot_checks_red,
        "recent_plot_checks": recent_plot_checks,
    }
    return templates.TemplateResponse(request, "dashboard.html", context)
