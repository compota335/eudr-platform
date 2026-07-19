"""ORM models for the EUDR pipeline.

Importing this package registers every model on the declarative ``Base``, so
``Base.metadata`` is complete for table creation and Alembic autogeneration.
"""

from __future__ import annotations

from app.models.alert import Alert
from app.models.base import Base, TimestampMixin, utcnow
from app.models.client import Client
from app.models.dds import DDS
from app.models.document import Document
from app.models.enums import (
    AlertSource,
    ClientSide,
    Commodity,
    CountryRiskTier,
    DDSStatus,
    DocumentKind,
    OutreachStatus,
    PlotStatus,
    RiskLevel,
    ShipmentStatus,
)
from app.models.evidence import Evidence
from app.models.outreach import OutreachMessage
from app.models.plot import Plot
from app.models.shipment import Shipment, shipment_plot
from app.models.supplier import Supplier

__all__ = [
    "Base",
    "TimestampMixin",
    "utcnow",
    # models
    "Client",
    "Supplier",
    "Plot",
    "Document",
    "Shipment",
    "shipment_plot",
    "DDS",
    "Evidence",
    "OutreachMessage",
    "Alert",
    # enums
    "AlertSource",
    "ClientSide",
    "Commodity",
    "CountryRiskTier",
    "DDSStatus",
    "DocumentKind",
    "OutreachStatus",
    "PlotStatus",
    "RiskLevel",
    "ShipmentStatus",
]
