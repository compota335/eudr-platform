"""Multi-page PDF rendering of an assembled internal Due Diligence Statement.

The DDS assembly service (:mod:`app.services.dds_assembly`) turns a verified
shipment into a persisted :class:`~app.models.dds.DDS` row whose ``payload_json``
is the full statement (operator, commodity, scope, per-plot geolocation and the
deforestation-free assertion). This module renders that stored payload as a
print-ready A4 PDF.

Like :mod:`app.services.pdf`, the renderer is a PURE function of a single row:
it reads ``dds.payload_json`` and ``dds.created_at`` and does no I/O, no database
access, no network calls, and never reads the wall clock — the statement's
timestamp is ``dds.created_at``, never ``datetime.now()``. Combined with
ReportLab's ``invariant`` flag (which pins the PDF's internal creation date to a
fixed epoch), the output is byte-for-byte deterministic given a fixed row.

We render STRICTLY from the stored payload, never re-deriving fields from the
ORM: the payload is the assembled record of what the statement says, and the PDF
must mirror it exactly. It is an INTERNAL document — no TRACES submission is
performed, so the footer states plainly that the reference is not a TRACES
reference number.

Fail-loud contract (see the house rules): if ``payload_json`` is missing or
unparseable, the reference number is absent, or the payload lacks a section a
coherent statement requires (operator / commodity / geolocation /
deforestation_free), the function raises ``ValueError`` rather than emitting a
misleading document. Fields that may legitimately be absent (eori, area,
centroid, commodity, country) are rendered as ``"-"`` and are NOT errors.
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.models.dds import DDS

# The plot-checker PDF is the canonical ReportLab renderer for this codebase.
# Reuse its module-public, render-agnostic helpers and palette rather than
# duplicating them, so both documents stay visually and behaviourally aligned.
from app.services.pdf import (
    _HEADER_BG,
    _RULE,
    _VERDICT_COLORS,
    _dash,
    _escape,
    _format_area,
    _format_timestamp,
    _stylesheet,
)

# Sections the stored payload MUST contain for a coherent statement. Absent, the
# document would be misleading, so rendering fails loud (see ``_validate_inputs``).
_REQUIRED_PAYLOAD_KEYS: tuple[str, ...] = (
    "operator",
    "commodity",
    "geolocation",
    "deforestation_free",
)

_DISCLAIMER = (
    "EUDR compliance tooling. Not legal advice. Regulation (EU) 2023/1115."
)
# The single most important honesty line on this document: it is an internal
# assembly, not a filed TRACES statement.
_NOT_TRACES_NOTE = (
    "Internal reference — not a TRACES reference number "
    "(TRACES submission not performed)."
)


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #
def render_dds_pdf(dds: DDS) -> bytes:
    """Render a print-ready A4 PDF of the assembled DDS and return its bytes.

    The document carries a reference/status header, the operator block, the
    commodity block, the scope block, a geolocation table (one row per plot),
    the deforestation-free assertion, and the legal footer. It may span several
    pages when the shipment has many plots. It is a pure function of the row:
    the same ``dds`` in, byte-identical PDF out (built with ReportLab's
    ``invariant`` flag, so no build timestamp leaks in).

    Raises:
        ValueError: if ``dds.payload_json`` is missing or unparseable, the
            reference number is absent, or the payload lacks a required section.
            Absent optional fields (eori, area, centroid, commodity, country)
            render as ``"-"`` and do not raise.
    """
    payload = _validate_inputs(dds)

    styles = _stylesheet()
    story: list[object] = [
        Paragraph("EUDR Due Diligence Statement", styles["ReportTitle"]),
        Spacer(1, 6 * mm),
        _reference_table(dds, payload, styles),
        Spacer(1, 6 * mm),
        Paragraph("Operator", styles["SectionHeading"]),
        Spacer(1, 2 * mm),
        _operator_table(payload["operator"], styles),
        Spacer(1, 6 * mm),
        Paragraph("Commodity", styles["SectionHeading"]),
        Spacer(1, 2 * mm),
        _commodity_table(payload["commodity"], styles),
        Spacer(1, 6 * mm),
        Paragraph("Scope", styles["SectionHeading"]),
        Spacer(1, 2 * mm),
        _scope_table(payload.get("scope") or {}, styles),
        Spacer(1, 6 * mm),
        Paragraph("Geolocation", styles["SectionHeading"]),
        Spacer(1, 2 * mm),
        _geolocation_table(payload["geolocation"], styles),
        Spacer(1, 6 * mm),
        Paragraph("Deforestation-free", styles["SectionHeading"]),
        Spacer(1, 2 * mm),
        *_deforestation_flowables(payload["deforestation_free"], styles),
        Spacer(1, 8 * mm),
        *_footer_flowables(styles),
    ]

    buffer = io.BytesIO()
    _build_document(buffer, story)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Validation — fail loud before a single flowable is built                     #
# --------------------------------------------------------------------------- #
def _validate_inputs(dds: DDS) -> dict[str, Any]:
    """Parse and validate the payload; return it, or raise ``ValueError``.

    Guards only what a coherent statement needs: a usable ``created_at``, a
    parseable JSON payload that is an object, a reference number, and the
    required top-level sections. Optional fields inside those sections render as
    ``"-"`` when absent and are intentionally NOT checked here.
    """
    if not isinstance(dds.created_at, datetime):
        raise ValueError(
            "DDS.created_at must be a datetime to render the statement; "
            f"got {type(dds.created_at).__name__}"
        )
    if not dds.payload_json:
        raise ValueError("DDS.payload_json is required to render the statement")
    try:
        payload = json.loads(dds.payload_json)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"DDS.payload_json is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(
            "DDS.payload_json must decode to a JSON object; "
            f"got {type(payload).__name__}"
        )
    if not payload.get("reference_number"):
        raise ValueError(
            "DDS payload is missing 'reference_number'; cannot render the statement"
        )
    missing = [key for key in _REQUIRED_PAYLOAD_KEYS if key not in payload]
    if missing:
        raise ValueError(
            f"DDS payload is missing required section(s): {', '.join(missing)}"
        )
    return payload


# --------------------------------------------------------------------------- #
# Sections                                                                      #
# --------------------------------------------------------------------------- #
def _reference_table(
    dds: DDS, payload: dict[str, Any], styles: dict[str, ParagraphStyle]
) -> Table:
    """Header key/value block: reference number, status, and assembly time."""
    rows = [
        ("Reference number", _text(payload.get("reference_number"))),
        ("Status", _dash(dds.status.value if dds.status is not None else None)),
        ("Assembled", _format_timestamp(dds.created_at)),
        ("Schema version", _text(payload.get("schema_version"))),
    ]
    return _kv_table(rows, styles)


def _operator_table(
    operator: dict[str, Any], styles: dict[str, ParagraphStyle]
) -> Table:
    """The declaring operator block (name, country, EORI, contact)."""
    rows = [
        ("Name", _text(operator.get("name"))),
        ("Country", _text(operator.get("country"))),
        ("EORI", _text(operator.get("eori"))),
        ("Contact email", _text(operator.get("contact_email"))),
    ]
    return _kv_table(rows, styles)


def _commodity_table(
    commodity: dict[str, Any], styles: dict[str, ParagraphStyle]
) -> Table:
    """The mandatory commodity line (commodity, CN code, quantity, origin)."""
    rows = [
        ("Commodity", _text(commodity.get("commodity"))),
        ("CN code", _text(commodity.get("cn_code"))),
        ("Quantity (kg)", _format_quantity(commodity.get("quantity_kg"))),
        ("Country of production", _text(commodity.get("country_of_production"))),
    ]
    return _kv_table(rows, styles)


def _scope_table(scope: dict[str, Any], styles: dict[str, ParagraphStyle]) -> Table:
    """The EUDR scope determination block, with its table versions."""
    rows = [
        ("In scope", _format_bool(scope.get("in_scope"))),
        ("Matched Annex I heading", _text(scope.get("matched_cn"))),
        ("Country risk", _text(scope.get("country_risk"))),
        ("CN table version", _text(scope.get("cn_table_version"))),
        ("Country table version", _text(scope.get("country_table_version"))),
    ]
    return _kv_table(rows, styles)


def _geolocation_table(
    geolocation: list[dict[str, Any]], styles: dict[str, ParagraphStyle]
) -> Table:
    """One row per plot: ref, supplier, country, area, geometry type, risk.

    An empty geolocation list is a contract violation for a real statement, but
    the payload validator already guarantees the section is present; if it is
    empty here a single explanatory row is emitted so the table is never blank.
    Rows are taken verbatim from the payload — none is ever fabricated.
    """
    header = [
        Paragraph("Ref", styles["CellHeader"]),
        Paragraph("Supplier", styles["CellHeader"]),
        Paragraph("Country", styles["CellHeader"]),
        Paragraph("Area (ha)", styles["CellHeader"]),
        Paragraph("Geometry", styles["CellHeader"]),
        Paragraph("Risk", styles["CellHeader"]),
    ]
    data: list[list[object]] = [header]
    style_cmds: list[tuple[object, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.25, _RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]

    if not geolocation:
        data.append([Paragraph("no plots recorded", styles["Cell"]), "", "", "", "", ""])
        style_cmds.append(("SPAN", (0, 1), (-1, 1)))
    else:
        for index, plot in enumerate(geolocation, start=1):
            risk = _text(plot.get("risk_level"))
            data.append(
                [
                    Paragraph(_escape(_text(plot.get("external_ref"))), styles["Cell"]),
                    Paragraph(_escape(_text(plot.get("supplier_name"))), styles["Cell"]),
                    Paragraph(_escape(_text(plot.get("country"))), styles["Cell"]),
                    Paragraph(_escape(_format_area(plot.get("area_ha"))), styles["Cell"]),
                    Paragraph(_escape(_text(plot.get("geometry_type"))), styles["Cell"]),
                    Paragraph(_escape(risk.upper()), styles["Cell"]),
                ]
            )
            colour = _risk_colour(plot.get("risk_level"))
            if colour is not None:
                style_cmds.append(("TEXTCOLOR", (5, index), (5, index), colour))

    table = Table(
        data,
        colWidths=[28 * mm, 46 * mm, 18 * mm, 22 * mm, 30 * mm, 26 * mm],
        repeatRows=1,
    )
    table.setStyle(TableStyle(style_cmds))
    return table


def _deforestation_flowables(
    deforestation_free: dict[str, Any], styles: dict[str, ParagraphStyle]
) -> list[object]:
    """The deforestation-free block: cutoff, ruleset version, and assertion."""
    rows = [
        ("Cutoff date", _text(deforestation_free.get("cutoff_date"))),
        ("Ruleset version", _text(deforestation_free.get("ruleset_version"))),
    ]
    assertion = _text(deforestation_free.get("assertion"))
    return [
        _kv_table(rows, styles),
        Spacer(1, 2 * mm),
        Paragraph(_escape(assertion), styles["Body"]),
    ]


def _footer_flowables(styles: dict[str, ParagraphStyle]) -> list[object]:
    """Render the legal footer, including the not-a-TRACES-reference notice."""
    return [
        Paragraph(_NOT_TRACES_NOTE, styles["Footer"]),
        Spacer(1, 1 * mm),
        Paragraph(_DISCLAIMER, styles["Footer"]),
    ]


# --------------------------------------------------------------------------- #
# Shared table builder                                                          #
# --------------------------------------------------------------------------- #
def _kv_table(
    rows: list[tuple[str, str]], styles: dict[str, ParagraphStyle]
) -> Table:
    """Build a two-column key/value table, styled like the plot-check metadata."""
    data = [
        [Paragraph(key, styles["CellHeader"]), Paragraph(_escape(value), styles["Cell"])]
        for key, value in rows
    ]
    table = Table(data, colWidths=[50 * mm, 120 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), _HEADER_BG),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, _RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


# --------------------------------------------------------------------------- #
# Document build                                                                #
# --------------------------------------------------------------------------- #
def _build_document(buffer: io.BytesIO, story: list[object]) -> None:
    """Build the A4 document into ``buffer``.

    ``invariant=1`` makes ReportLab omit the wall-clock build timestamp (it pins
    the PDF's internal creation date to a fixed epoch), so two builds of the same
    story produce byte-identical output. Combined with the renderer never calling
    ``datetime.now()``, this is what makes the export deterministic.
    """
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="EUDR Due Diligence Statement",
        author="EUDR platform",
        subject="Internal Due Diligence Statement (unverified, not filed to TRACES)",
        invariant=1,
    )
    doc.build(story)


# --------------------------------------------------------------------------- #
# Formatting helpers — pure string shaping, no I/O                             #
# --------------------------------------------------------------------------- #
def _text(value: Any) -> str:
    """Render any scalar payload value as text, using ``"-"`` for None/blank.

    A thin wrapper over :func:`app.services.pdf._dash` that first stringifies
    non-string scalars (the payload is JSON, so a value may be a number or bool).
    """
    if value is None:
        return "-"
    return _dash(value if isinstance(value, str) else str(value))


def _format_quantity(quantity_kg: Any) -> str:
    """Render an optional kilogram quantity, or ``"-"`` when absent."""
    if quantity_kg is None:
        return "-"
    if isinstance(quantity_kg, (int, float)):
        return f"{quantity_kg:g}"
    return _text(quantity_kg)


def _format_bool(value: Any) -> str:
    """Render an optional boolean as Yes/No, or ``"-"`` when absent."""
    if value is None:
        return "-"
    return "Yes" if value else "No"


def _risk_colour(risk_level: Any) -> colors.Color | None:
    """Map a payload risk level string to its verdict colour, or ``None``.

    Reuses the plot-checker palette keyed by :class:`RiskLevel`; an unknown or
    missing value yields ``None`` (the cell then keeps the default ink colour)
    rather than raising, since the risk text itself is always shown.
    """
    if not isinstance(risk_level, str):
        return None
    for level, colour in _VERDICT_COLORS.items():
        if level.value == risk_level:
            return colour
    return None
