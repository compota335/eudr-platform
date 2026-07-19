"""Tests for the public plot-checker HTTP vertical.

The deforestation provider is injected via ``app.dependency_overrides`` so the
whole pipeline runs WITHOUT a network call and WITHOUT a Whisp key: a fake
returns canned :class:`PlotEvidence`, and a second fake raises
``RiskProviderNotConfigured`` to prove the fail-loud path stores nothing and
never fabricates a verdict.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.geo.schemas import (
    DatasetSignal,
    PlotEvidence,
    RiskProviderNotConfigured,
    RiskResult,
)
from app.main import app
from app.models.enums import RiskLevel
from app.models.plot_check import PlotCheck
from app.routers.plot_checker import get_risk_provider
from app.services.serialization import (
    deserialize_risk_result,
    serialize_risk_result,
)

# A valid single-plot GeoJSON polygon (a small square, ~1.2 ha near the equator).
_POLYGON = json.dumps(
    {
        "type": "Polygon",
        "coordinates": [
            [
                [-5.100, 7.600],
                [-5.099, 7.600],
                [-5.099, 7.601],
                [-5.100, 7.601],
                [-5.100, 7.600],
            ]
        ],
    }
)


# --------------------------------------------------------------------------- #
# Fake providers                                                               #
# --------------------------------------------------------------------------- #
def _red_evidence(geometry: dict, *, external_ref: str | None = None) -> PlotEvidence:
    """Two independent post-2020 loss families -> a RED verdict."""
    return PlotEvidence(
        forest_2020=(
            DatasetSignal(dataset="EUFO_2020", family="EUFO", value=1.0, kind="forest_2020"),
        ),
        loss_after_2020=(
            DatasetSignal(
                dataset="GFC_loss_after_2020", family="GFC", value=0.5,
                kind="loss_after_2020",
            ),
            DatasetSignal(
                dataset="RADD_after_2020", family="RADD", value=0.3,
                kind="loss_after_2020",
            ),
        ),
        provider="fake",
        dataset_versions={"fake": "v1"},
        raw={"note": "canned"},
    )


def _raise_not_configured(geometry: dict, *, external_ref: str | None = None) -> PlotEvidence:
    raise RiskProviderNotConfigured("no key in test")


@pytest.fixture
def red_provider() -> Iterator[None]:
    app.dependency_overrides[get_risk_provider] = lambda: _red_evidence
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_risk_provider, None)


@pytest.fixture
def unconfigured_provider() -> Iterator[None]:
    app.dependency_overrides[get_risk_provider] = lambda: _raise_not_configured
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_risk_provider, None)


# --------------------------------------------------------------------------- #
# Page                                                                         #
# --------------------------------------------------------------------------- #
def test_plot_checker_page_renders(client: TestClient) -> None:
    response = client.get("/plot-checker")
    assert response.status_code == 200
    assert "Plot deforestation check" in response.text


# --------------------------------------------------------------------------- #
# Happy path                                                                   #
# --------------------------------------------------------------------------- #
def test_check_plot_valid_geojson_persists_and_returns_json(
    client: TestClient, session: Session, red_provider: None
) -> None:
    response = client.post(
        "/api/check-plot",
        data={"geojson": _POLYGON, "commodity": "cocoa", "country": "ci"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["level"] == "red"
    assert body["token"]
    assert body["converging_families"] == ["GFC", "RADD"]
    assert body["email_required_for_pdf"] is True

    row = (
        session.query(PlotCheck)
        .filter(PlotCheck.token == body["token"])
        .one_or_none()
    )
    assert row is not None
    assert row.risk_level is RiskLevel.red
    assert row.country == "CI"
    assert row.provider == "fake"
    assert row.result_json is not None


def test_check_plot_htmx_returns_fragment(
    client: TestClient, red_provider: None
) -> None:
    response = client.post(
        "/api/check-plot",
        data={"geojson": _POLYGON},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "RED" in response.text
    # The email-capture form must be present so the visitor can unlock the PDF.
    assert "/email" in response.text


def test_check_plot_multiple_geometries_discloses_note(
    client: TestClient, red_provider: None
) -> None:
    two = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": {}, "geometry": json.loads(_POLYGON)},
                {"type": "Feature", "properties": {}, "geometry": json.loads(_POLYGON)},
            ],
        }
    )
    response = client.post("/api/check-plot", data={"geojson": two})
    assert response.status_code == 200
    notes = response.json()["notes"]
    assert any("2 geometries detected" in note for note in notes)


# --------------------------------------------------------------------------- #
# Fail-loud paths                                                              #
# --------------------------------------------------------------------------- #
def test_check_plot_no_input_is_400(client: TestClient, red_provider: None) -> None:
    response = client.post("/api/check-plot", data={})
    assert response.status_code == 400


def test_check_plot_malformed_geometry_is_400(
    client: TestClient, red_provider: None
) -> None:
    response = client.post("/api/check-plot", data={"geojson": "{not valid geojson"})
    assert response.status_code == 400
    assert "error" in response.json()


def test_check_plot_provider_not_configured_is_503_and_stores_nothing(
    client: TestClient, session: Session, unconfigured_provider: None
) -> None:
    response = client.post("/api/check-plot", data={"geojson": _POLYGON})
    assert response.status_code == 503
    body = response.json()
    # Fail loud: only an error message, never a fabricated verdict.
    assert set(body) == {"error"}
    assert "level" not in body
    assert "provider" in body["error"].lower()
    # Nothing was persisted.
    assert session.query(PlotCheck).count() == 0


# --------------------------------------------------------------------------- #
# Email capture + gated PDF                                                    #
# --------------------------------------------------------------------------- #
def _create_check(client: TestClient, provider: None) -> str:
    response = client.post("/api/check-plot", data={"geojson": _POLYGON})
    assert response.status_code == 200
    return response.json()["token"]


def test_email_capture_happy_path_persists_email(
    client: TestClient, session: Session, red_provider: None
) -> None:
    token = _create_check(client, red_provider)
    response = client.post(
        f"/api/check-plot/{token}/email", data={"email": "buyer@example.com"}
    )
    assert response.status_code == 200
    assert response.json()["email_captured"] is True

    row = session.query(PlotCheck).filter(PlotCheck.token == token).one()
    assert row.email == "buyer@example.com"


def test_email_capture_rejects_bad_email(
    client: TestClient, red_provider: None
) -> None:
    token = _create_check(client, red_provider)
    response = client.post(
        f"/api/check-plot/{token}/email", data={"email": "not-an-email"}
    )
    assert response.status_code == 400


def test_email_capture_unknown_token_is_404(
    client: TestClient, red_provider: None
) -> None:
    response = client.post(
        "/api/check-plot/does-not-exist/email", data={"email": "a@b.com"}
    )
    assert response.status_code == 404


def test_pdf_before_email_is_403(client: TestClient, red_provider: None) -> None:
    token = _create_check(client, red_provider)
    response = client.get(f"/api/check-plot/{token}/pdf")
    assert response.status_code == 403


def test_pdf_after_email_is_pdf(
    client: TestClient, red_provider: None
) -> None:
    token = _create_check(client, red_provider)
    client.post(f"/api/check-plot/{token}/email", data={"email": "buyer@example.com"})
    response = client.get(f"/api/check-plot/{token}/pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    assert f"eudr-plot-{token}.pdf" in response.headers["content-disposition"]


def test_pdf_unknown_token_is_404(client: TestClient) -> None:
    response = client.get("/api/check-plot/nope/pdf")
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# RiskResult serialization round-trip                                          #
# --------------------------------------------------------------------------- #
def test_risk_result_round_trip_is_lossless() -> None:
    evidence = _red_evidence({})
    from app.services.risk import assess

    result: RiskResult = assess(evidence)
    restored = deserialize_risk_result(serialize_risk_result(result))
    assert restored == result
