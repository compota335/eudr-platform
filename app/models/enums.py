"""Enumerations shared across the data model.

All enums subclass ``enum.StrEnum`` so values serialise cleanly to JSON and
store as plain text (``native_enum=False``) for SQLite/PostgreSQL portability.
Member names equal their values, which is also what SQLAlchemy persists.
"""

from __future__ import annotations

import enum


class ClientSide(enum.StrEnum):
    """Which side of the supply chain the client sits on."""

    importer_eu = "importer_eu"
    exporter_sa = "exporter_sa"


class Commodity(enum.StrEnum):
    """EUDR Annex I commodities."""

    cattle = "cattle"
    cocoa = "cocoa"
    coffee = "coffee"
    oil_palm = "oil_palm"
    rubber = "rubber"
    soya = "soya"
    wood = "wood"


class PlotStatus(enum.StrEnum):
    """Lifecycle state of a plot geometry."""

    pending = "pending"
    valid = "valid"
    rejected = "rejected"
    needs_review = "needs_review"


class RiskLevel(enum.StrEnum):
    """Deforestation risk verdict (green / amber / red)."""

    green = "green"
    amber = "amber"
    red = "red"


class CountryRiskTier(enum.StrEnum):
    """Commission country benchmarking tier."""

    low = "low"
    standard = "standard"
    high = "high"


class DocumentKind(enum.StrEnum):
    """Category of an uploaded document."""

    geometry = "geometry"
    land_title = "land_title"
    permit = "permit"
    certificate = "certificate"
    other = "other"


class OutreachStatus(enum.StrEnum):
    """State of a supplier outreach thread."""

    pending = "pending"
    sent = "sent"
    reminded = "reminded"
    responded = "responded"
    escalated = "escalated"
    failed = "failed"


class ShipmentStatus(enum.StrEnum):
    """State of a shipment / batch."""

    pending = "pending"
    ready = "ready"
    filed = "filed"
    blocked = "blocked"


class DDSStatus(enum.StrEnum):
    """State of a Due Diligence Statement."""

    draft = "draft"
    assembled = "assembled"
    submitted = "submitted"
    accepted = "accepted"
    rejected = "rejected"


class AlertSource(enum.StrEnum):
    """Source dataset of a deforestation alert."""

    glad = "glad"
    radd = "radd"
    hansen = "hansen"
