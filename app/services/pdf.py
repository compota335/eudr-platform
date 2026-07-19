"""One-page PDF risk report for a public plot check.

The public plot-checker (``PlotCheck``) is a lead magnet: a visitor uploads a
geometry, the pipeline returns a green/amber/red deforestation verdict, and the
visitor may then export that verdict as a PDF. This module renders that PDF.

The renderer is a PURE function of its two inputs: the ``PlotCheck`` row and the
``RiskResult`` the risk engine produced for it. It performs no I/O, no database
access and no network calls, and it never reads the wall clock — the report's
timestamp is ``check.created_at``, never ``datetime.now()``. That makes the
output unit-testable and, thanks to ReportLab's ``invariant`` flag, byte-for-byte
deterministic given fixed inputs (the flag pins the PDF's internal creation date
to a fixed epoch instead of the build time; see ``_build_document``).

Fail-loud contract (see the house rules): if ``check`` and ``result`` describe
different verdicts, or a field required to render a coherent report is missing,
the function raises ``ValueError`` rather than emitting a misleading PDF. Fields
that may legitimately be absent (area, centroid, commodity, country) are rendered
as ``"-"`` and are NOT errors.
"""

from __future__ import annotations

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.geo.schemas import DatasetSignal, PlotEvidence, RiskResult
from app.models.enums import RiskLevel
from app.models.plot_check import PlotCheck

# --------------------------------------------------------------------------- #
# Palette — one accent colour per verdict, matched to the web UI badges.       #
# --------------------------------------------------------------------------- #
# Kept as ReportLab HexColor objects so they can be dropped straight into a
# TableStyle or a Paragraph background without further conversion.
_VERDICT_COLORS: dict[RiskLevel, colors.Color] = {
    RiskLevel.green: colors.HexColor("#059669"),
    RiskLevel.amber: colors.HexColor("#d97706"),
    RiskLevel.red: colors.HexColor("#dc2626"),
}

_INK = colors.HexColor("#111827")  # near-black body text
_MUTED = colors.HexColor("#6b7280")  # secondary/footer text
_RULE = colors.HexColor("#e5e7eb")  # light table grid lines
_HEADER_BG = colors.HexColor("#f3f4f6")  # table header fill

# The four evidence buckets, in the order they appear in the report, paired with
# the human-readable heading each one carries above its row block.
_EVIDENCE_BUCKETS: tuple[tuple[str, str], ...] = (
    ("forest_2020", "Baseline forest (2020)"),
    ("loss_after_2020", "Loss after 2020 (in plot)"),
    ("loss_after_2020_buffer", "Loss after 2020 (buffer)"),
    ("commodity_2020", "Commodity land cover (2020)"),
)

_DISCLAIMER = (
    "EUDR compliance tooling. Not legal advice. Regulation (EU) 2023/1115."
)
_SATELLITE_NOTE = (
    "Satellite-derived results are provided as-is, without warranty of "
    "completeness or fitness for a particular purpose."
)


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #
def render_plot_check_pdf(check: PlotCheck, result: RiskResult) -> bytes:
    """Render a one-page A4 PDF risk report for ``check`` and return its bytes.

    The report carries a coloured verdict banner, a metadata block, the risk
    rationale, an evidence table over the four dataset buckets, and a legal
    footer. It is a pure function of its inputs: same ``check`` and ``result``
    in, byte-identical PDF out (the document is built with ReportLab's
    ``invariant`` flag, so no build timestamp leaks in).

    Raises:
        ValueError: if ``check`` and ``result`` disagree on the verdict, or a
            field required for a coherent report is missing. Absent area,
            centroid, commodity or country are rendered as ``"-"`` and do not
            raise.
    """
    _validate_inputs(check, result)

    styles = _stylesheet()
    story: list[object] = [
        Paragraph("EUDR Deforestation Risk Report", styles["ReportTitle"]),
        Spacer(1, 6 * mm),
        _verdict_banner(result.level, styles),
        Spacer(1, 6 * mm),
        Paragraph("Assessment details", styles["SectionHeading"]),
        Spacer(1, 2 * mm),
        _metadata_table(check, result),
        Spacer(1, 6 * mm),
        Paragraph("Rationale", styles["SectionHeading"]),
        Spacer(1, 2 * mm),
        *_rationale_flowables(result, styles),
        Spacer(1, 6 * mm),
        Paragraph("Evidence", styles["SectionHeading"]),
        Spacer(1, 2 * mm),
        _evidence_table(result.evidence, styles),
        Spacer(1, 8 * mm),
        *_footer_flowables(styles),
    ]

    buffer = io.BytesIO()
    _build_document(buffer, story)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Validation — fail loud before a single flowable is built                     #
# --------------------------------------------------------------------------- #
def _validate_inputs(check: PlotCheck, result: RiskResult) -> None:
    """Raise ``ValueError`` if the report cannot be rendered coherently.

    Guards only the fields whose absence or mismatch would make the report
    misleading: the token, a timezone-usable ``created_at``, the verdict level,
    a ruleset version, and agreement between ``check.risk_level`` and
    ``result.level`` when the row already recorded a verdict. Area, centroid,
    commodity and country are intentionally NOT checked here — they render as
    ``"-"`` when absent.
    """
    if not check.token:
        raise ValueError("PlotCheck.token is required to render the report")
    if not isinstance(check.created_at, datetime):
        raise ValueError(
            "PlotCheck.created_at must be a datetime to render the report; "
            f"got {type(check.created_at).__name__}"
        )
    if result.level not in _VERDICT_COLORS:
        raise ValueError(f"unknown risk level {result.level!r}; cannot colour the banner")
    if not result.ruleset_version:
        raise ValueError("RiskResult.ruleset_version is required to render the report")
    # If the row already persisted a verdict, it must match the result being
    # rendered; a report claiming one level over a row stamped with another is a
    # data-integrity bug we refuse to paper over.
    if check.risk_level is not None and check.risk_level != result.level:
        raise ValueError(
            "PlotCheck.risk_level "
            f"({check.risk_level.value}) disagrees with RiskResult.level "
            f"({result.level.value}); refusing to render a contradictory report"
        )


# --------------------------------------------------------------------------- #
# Styles                                                                        #
# --------------------------------------------------------------------------- #
def _stylesheet() -> dict[str, ParagraphStyle]:
    """Return the paragraph styles used across the report, keyed by name.

    Built on ReportLab's sample sheet but with a fixed, explicit set of custom
    styles so the layout does not drift with library defaults.
    """
    base = getSampleStyleSheet()
    styles: dict[str, ParagraphStyle] = {}

    styles["ReportTitle"] = ParagraphStyle(
        "ReportTitle",
        parent=base["Title"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=_INK,
        spaceAfter=0,
    )
    styles["SectionHeading"] = ParagraphStyle(
        "SectionHeading",
        parent=base["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=15,
        textColor=_INK,
        spaceBefore=0,
        spaceAfter=0,
    )
    styles["Banner"] = ParagraphStyle(
        "Banner",
        parent=base["Normal"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        alignment=TA_CENTER,
        textColor=colors.white,
    )
    styles["Body"] = ParagraphStyle(
        "Body",
        parent=base["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=13,
        textColor=_INK,
    )
    styles["Bullet"] = ParagraphStyle(
        "Bullet",
        parent=styles["Body"],
        leftIndent=10,
        bulletIndent=0,
        spaceAfter=2,
    )
    styles["Cell"] = ParagraphStyle(
        "Cell",
        parent=styles["Body"],
        fontSize=8.5,
        leading=11,
    )
    styles["CellHeader"] = ParagraphStyle(
        "CellHeader",
        parent=styles["Cell"],
        fontName="Helvetica-Bold",
    )
    styles["Footer"] = ParagraphStyle(
        "Footer",
        parent=base["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=10,
        textColor=_MUTED,
    )
    return styles


# --------------------------------------------------------------------------- #
# Sections                                                                      #
# --------------------------------------------------------------------------- #
def _verdict_banner(level: RiskLevel, styles: dict[str, ParagraphStyle]) -> Table:
    """Build the full-width coloured banner showing the verdict in uppercase."""
    label = f"RISK: {level.value.upper()}"
    banner = Table(
        [[Paragraph(label, styles["Banner"])]],
        colWidths=[170 * mm],
        rowHeights=[16 * mm],
    )
    banner.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), _VERDICT_COLORS[level]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return banner


def _metadata_table(check: PlotCheck, result: RiskResult) -> Table:
    """Build the two-column key/value table describing the checked plot.

    Optional fields (area, centroid, commodity, country, provider) render as
    ``"-"`` when absent; that is deliberate and not an error.
    """
    rows = [
        ("Check token", check.token),
        ("Created", _format_timestamp(check.created_at)),
        ("Source format", _dash(check.source_format)),
        ("Geometry type", _dash(check.geometry_type)),
        ("Area (ha)", _format_area(check.area_ha)),
        ("Centroid (lon, lat)", _format_centroid(check.centroid_lon, check.centroid_lat)),
        ("Commodity", _dash(check.commodity.value if check.commodity else None)),
        ("Country", _dash(check.country)),
        ("Provider", _dash(check.provider)),
        ("Ruleset version", result.ruleset_version),
    ]
    styles = _stylesheet()
    data = [
        [Paragraph(key, styles["CellHeader"]), Paragraph(_escape(value), styles["Cell"])]
        for key, value in rows
    ]
    table = Table(data, colWidths=[45 * mm, 125 * mm])
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


def _rationale_flowables(
    result: RiskResult, styles: dict[str, ParagraphStyle]
) -> list[object]:
    """Render one bullet per line in ``result.rationale``.

    An empty rationale is a contract violation for a real verdict, but not a
    reason to crash the export: a single explanatory bullet is emitted instead
    so the section is never blank.
    """
    if not result.rationale:
        return [Paragraph("- No rationale recorded for this verdict.", styles["Bullet"])]
    return [
        Paragraph(f"- {_escape(line)}", styles["Bullet"]) for line in result.rationale
    ]


def _evidence_table(evidence: PlotEvidence, styles: dict[str, ParagraphStyle]) -> Table:
    """Build the evidence table over the four dataset buckets.

    Columns are Dataset | Family | Kind | Area (ha). Each bucket is introduced by
    a spanning heading row; an empty bucket still renders its heading followed by
    a single "no signals" row. Rows are taken verbatim from the evidence — no
    row is ever fabricated.
    """
    header = [
        Paragraph("Dataset", styles["CellHeader"]),
        Paragraph("Family", styles["CellHeader"]),
        Paragraph("Kind", styles["CellHeader"]),
        Paragraph("Area (ha)", styles["CellHeader"]),
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

    for field_name, heading in _EVIDENCE_BUCKETS:
        signals: tuple[DatasetSignal, ...] = getattr(evidence, field_name)
        heading_row = len(data)
        data.append([Paragraph(heading, styles["CellHeader"]), "", "", ""])
        style_cmds.append(("SPAN", (0, heading_row), (-1, heading_row)))
        style_cmds.append(("BACKGROUND", (0, heading_row), (-1, heading_row), _RULE))
        if not signals:
            data.append([Paragraph("no signals", styles["Cell"]), "", "", ""])
            style_cmds.append(("SPAN", (0, len(data) - 1), (-1, len(data) - 1)))
            continue
        for signal in signals:
            data.append(
                [
                    Paragraph(_escape(signal.dataset), styles["Cell"]),
                    Paragraph(_escape(signal.family), styles["Cell"]),
                    Paragraph(_escape(signal.kind), styles["Cell"]),
                    Paragraph(f"{signal.value:.2f}", styles["Cell"]),
                ]
            )

    table = Table(data, colWidths=[70 * mm, 30 * mm, 45 * mm, 25 * mm], repeatRows=1)
    table.setStyle(TableStyle(style_cmds))
    return table


def _footer_flowables(styles: dict[str, ParagraphStyle]) -> list[object]:
    """Render the two-line legal footer."""
    return [
        Paragraph(_DISCLAIMER, styles["Footer"]),
        Spacer(1, 1 * mm),
        Paragraph(_SATELLITE_NOTE, styles["Footer"]),
    ]


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
        title="EUDR Deforestation Risk Report",
        author="EUDR platform",
        subject="Plot deforestation risk assessment",
        invariant=1,
    )
    doc.build(story)


# --------------------------------------------------------------------------- #
# Formatting helpers — pure string shaping, no I/O                             #
# --------------------------------------------------------------------------- #
def _dash(value: str | None) -> str:
    """Render an optional string, using ``"-"`` for None or blank."""
    if value is None:
        return "-"
    text = value.strip()
    return text if text else "-"


def _format_area(area_ha: float | None) -> str:
    """Render an optional hectare area with four decimals, or ``"-"``."""
    if area_ha is None:
        return "-"
    return f"{area_ha:.4f}"


def _format_centroid(lon: float | None, lat: float | None) -> str:
    """Render the centroid as ``"lon, lat"`` with six decimals, or ``"-"``.

    Both coordinates must be present together; a half-specified centroid is
    meaningless, so if either is missing the whole field renders as ``"-"``.
    """
    if lon is None or lat is None:
        return "-"
    return f"{lon:.6f}, {lat:.6f}"


def _format_timestamp(moment: datetime) -> str:
    """Render ``created_at`` as an ISO-like UTC string, no wall-clock reads.

    The value comes straight from the row; this only formats it. Naive datetimes
    are rendered as-is (labelled ``UTC``) since the model stores UTC.
    """
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def _escape(value: str) -> str:
    """Escape XML metacharacters for ReportLab's mini-markup Paragraph parser.

    ReportLab parses Paragraph text as XML-like markup, so a raw ``&``, ``<`` or
    ``>`` in provider-supplied dataset names would break the build. Escaping is
    mandatory for correctness, not cosmetic.
    """
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
