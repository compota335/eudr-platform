"""Tests for the one-page PDF risk report (``app.services.pdf``).

These tests build the ORM ``PlotCheck`` row in memory (never persisted) with a
fixed ``created_at`` and construct ``RiskResult`` / ``PlotEvidence`` directly, so
the renderer is exercised as the pure, deterministic function it is meant to be.
Verifying text INSIDE the compiled PDF binary is out of scope; assertions stay at
the structural / byte level (``%PDF-`` magic, length floor, determinism).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.geo.schemas import DatasetSignal, PlotEvidence, RiskResult
from app.models.enums import Commodity, RiskLevel
from app.models.plot_check import PlotCheck
from app.services.pdf import render_plot_check_pdf

# A fixed instant so the report timestamp — and therefore the whole PDF — is
# reproducible across test runs.
_FIXED_CREATED_AT = datetime(2026, 7, 19, 8, 30, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Builders                                                                      #
# --------------------------------------------------------------------------- #
def _forest(family: str, value: float) -> DatasetSignal:
    return DatasetSignal(
        dataset=f"{family}_2020", family=family, value=value, kind="forest_2020"
    )


def _loss(family: str, value: float, dataset: str | None = None) -> DatasetSignal:
    return DatasetSignal(
        dataset=dataset or f"{family}_loss_after_2020",
        family=family,
        value=value,
        kind="loss_after_2020",
    )


def _make_check(
    level: RiskLevel | None,
    *,
    token: str = "tok_" + "a" * 39,
    commodity: Commodity | None = Commodity.cocoa,
    country: str | None = "CI",
) -> PlotCheck:
    """Construct an in-memory (unpersisted) PlotCheck with a fixed timestamp."""
    check = PlotCheck(
        token=token,
        source_format="geojson",
        geometry_geojson='{"type": "Polygon", "coordinates": []}',
        geometry_type="Polygon",
        area_ha=12.3456,
        centroid_lon=-5.123456,
        centroid_lat=7.654321,
        commodity=commodity,
        country=country,
        risk_level=level,
        ruleset_version="2026.07.1",
        provider="whisp",
    )
    # created_at normally defaults on flush; set it explicitly so the render is
    # deterministic without a database round-trip.
    check.created_at = _FIXED_CREATED_AT
    return check


def _green_result() -> RiskResult:
    evidence = PlotEvidence(forest_2020=(_forest("EUFO", 0.90),), provider="whisp")
    return RiskResult(
        level=RiskLevel.green,
        ruleset_version="2026.07.1",
        forest_2020_present=True,
        signals_in_plot=0,
        signals_in_buffer=0,
        converging_families=(),
        rationale=("GREEN: forest present at the 2020 cutoff with no post-2020 loss",),
        evidence=evidence,
    )


def _amber_result() -> RiskResult:
    evidence = PlotEvidence(
        forest_2020=(_forest("GFC", 0.80),),
        loss_after_2020=(_loss("GFC", 0.42),),
        provider="whisp",
    )
    return RiskResult(
        level=RiskLevel.amber,
        ruleset_version="2026.07.1",
        forest_2020_present=True,
        signals_in_plot=1,
        signals_in_buffer=0,
        converging_families=("GFC",),
        rationale=("AMBER: post-2020 loss in a single dataset (GFC); no corroboration",),
        evidence=evidence,
    )


def _red_result() -> RiskResult:
    evidence = PlotEvidence(
        forest_2020=(_forest("EUFO", 0.95), _forest("GFC", 0.90)),
        loss_after_2020=(_loss("GFC", 0.42), _loss("RADD", 0.31)),
        loss_after_2020_buffer=(_loss("TMF", 0.15),),
        commodity_2020=(
            DatasetSignal(
                dataset="Cocoa_ETH", family="cocoa", value=1.20, kind="commodity_2020"
            ),
        ),
        provider="whisp",
    )
    return RiskResult(
        level=RiskLevel.red,
        ruleset_version="2026.07.1",
        forest_2020_present=True,
        signals_in_plot=2,
        signals_in_buffer=1,
        converging_families=("GFC", "RADD"),
        rationale=(
            "RED: post-2020 loss converges across 2 independent datasets: "
            "GFC (0.42 ha), RADD (0.31 ha)",
        ),
        evidence=evidence,
    )


# --------------------------------------------------------------------------- #
# Basic rendering for each verdict                                             #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("level", "builder"),
    [
        (RiskLevel.green, _green_result),
        (RiskLevel.amber, _amber_result),
        (RiskLevel.red, _red_result),
    ],
)
def test_renders_pdf_for_each_verdict(level: RiskLevel, builder) -> None:  # noqa: ANN001
    check = _make_check(level)
    pdf = render_plot_check_pdf(check, builder())
    assert isinstance(pdf, bytes)
    assert pdf.startswith(b"%PDF-")
    # A one-page report with a banner, tables and footer is comfortably over 1 KB;
    # a much smaller blob would mean the story failed to build.
    assert len(pdf) > 1500


def test_output_is_byte_deterministic() -> None:
    # ReportLab is built with invariant=1 and the renderer never reads the wall
    # clock, so two builds of the same inputs must be byte-identical.
    check = _make_check(RiskLevel.red)
    result = _red_result()
    first = render_plot_check_pdf(check, result)
    second = render_plot_check_pdf(check, result)
    assert first == second
    assert first.startswith(b"%PDF-")


def test_empty_evidence_buckets_still_render() -> None:
    # A green verdict with only baseline forest leaves three buckets empty; the
    # table must still build (each empty bucket becomes a "no signals" row).
    check = _make_check(RiskLevel.green)
    pdf = render_plot_check_pdf(check, _green_result())
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1500


def test_optional_fields_none_render_as_dash() -> None:
    # Area/centroid/commodity/country absent is legitimate, not an error.
    check = _make_check(RiskLevel.green, commodity=None, country=None)
    check.area_ha = None
    check.centroid_lon = None
    check.centroid_lat = None
    check.provider = None
    pdf = render_plot_check_pdf(check, _green_result())
    assert pdf.startswith(b"%PDF-")


def test_red_result_with_full_evidence_renders() -> None:
    # The red fixture populates all four buckets (forest, loss, buffer,
    # commodity); the render must succeed end to end. Text inside the PDF binary
    # is intentionally not inspected — only that a valid PDF is produced.
    check = _make_check(RiskLevel.red)
    result = _red_result()
    assert result.evidence.loss_after_2020[0].family == "GFC"
    pdf = render_plot_check_pdf(check, result)
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1500


# --------------------------------------------------------------------------- #
# Fail-loud contract                                                            #
# --------------------------------------------------------------------------- #
def test_verdict_mismatch_raises() -> None:
    # Row stamped GREEN but result says RED: contradictory, must fail loud.
    check = _make_check(RiskLevel.green)
    with pytest.raises(ValueError, match="disagrees with RiskResult.level"):
        render_plot_check_pdf(check, _red_result())


def test_missing_token_raises() -> None:
    check = _make_check(RiskLevel.green, token="")
    with pytest.raises(ValueError, match="token is required"):
        render_plot_check_pdf(check, _green_result())


def test_created_at_not_datetime_raises() -> None:
    check = _make_check(RiskLevel.green)
    check.created_at = None  # type: ignore[assignment]
    with pytest.raises(ValueError, match="created_at must be a datetime"):
        render_plot_check_pdf(check, _green_result())


def test_null_row_risk_level_is_allowed() -> None:
    # A row whose verdict has not been persisted yet (risk_level is None) is not
    # a mismatch; the result's level drives the report.
    check = _make_check(None)
    pdf = render_plot_check_pdf(check, _amber_result())
    assert pdf.startswith(b"%PDF-")
