"""Tests for the Whisp API client (Stage 4 provider adapter, sync path).

Every HTTP interaction is mocked with respx; no test ever hits the live Whisp
API. The mocked responses mirror the SHAPES verified against the live API in
2026-07: the shared ``{"code", "message", "data"}`` envelope, ``analysis_completed``
on success with ``data`` a GeoJSON FeatureCollection, and non-2xx statuses that
carry the same envelope for auth / validation / server failures.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.geo.schemas import (
    PlotEvidence,
    RiskProviderError,
    RiskProviderNotConfigured,
)
from app.services.whisp import WhispClient, analyze_geometry

API_URL = "https://whisp.test/api"
KEY = "test-key"

# A minimal WGS84 polygon used across the happy-path tests.
POLYGON = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0]]],
}

# Whisp result columns reused from WHISP_COLUMN_FAMILIES in risk.py.
WHISP_PROPERTIES = {
    "GFC_loss_after_2020": 0.42,
    "RADD_after_2020": 0.31,
    "EUFO_2020": 0.90,
}


def _client(**overrides: object) -> WhispClient:
    """Build a client wired to the mocked API."""
    params: dict[str, object] = {
        "api_url": API_URL,
        "api_key": KEY,
        "client": httpx.Client(),
    }
    params.update(overrides)
    return WhispClient(**params)  # type: ignore[arg-type]


def _completed(properties: dict[str, object] | None = None) -> httpx.Response:
    """A 200 ``analysis_completed`` envelope wrapping one result feature."""
    feature = {
        "type": "Feature",
        "geometry": POLYGON,
        "properties": WHISP_PROPERTIES if properties is None else properties,
    }
    return httpx.Response(
        200,
        json={
            "code": "analysis_completed",
            "message": "Analysis completed successfully",
            "data": {"type": "FeatureCollection", "features": [feature]},
        },
    )


def _submit_ok(
    respx_mock: respx.MockRouter, properties: dict[str, object] | None = None
) -> None:
    respx_mock.post(f"{API_URL}/submit/geojson").mock(
        return_value=_completed(properties)
    )


# --------------------------------------------------------------------------- #
# Happy path                                                                   #
# --------------------------------------------------------------------------- #
@respx.mock
def test_happy_path_returns_plot_evidence(respx_mock: respx.MockRouter) -> None:
    _submit_ok(respx_mock)

    with _client() as client:
        evidence = client.analyze(POLYGON, external_ref="lot-7")

    assert isinstance(evidence, PlotEvidence)
    assert evidence.provider == "whisp"
    assert evidence.raw == WHISP_PROPERTIES
    assert evidence.dataset_versions == {"whisp_api": "v2.1.0"}
    # Signals carry the mocked Whisp column names and areas.
    families = {s.family: s for s in evidence.loss_after_2020}
    assert families["GFC"].dataset == "GFC_loss_after_2020"
    assert families["GFC"].value == pytest.approx(0.42)
    assert families["RADD"].value == pytest.approx(0.31)
    assert {s.dataset for s in evidence.forest_2020} == {"EUFO_2020"}


@respx.mock
def test_submit_payload_shape(respx_mock: respx.MockRouter) -> None:
    _submit_ok(respx_mock)

    with _client() as client:
        client.analyze(POLYGON, external_ref="lot-7")

    request = respx_mock.calls[0].request
    assert request.headers["x-api-key"] == KEY
    import json

    body = json.loads(request.content)
    assert body["type"] == "FeatureCollection"
    assert body["features"][0]["geometry"] == POLYGON
    # Sync path in hectares, with the external ref carried and named.
    assert body["analysisOptions"]["async"] is False
    assert body["analysisOptions"]["unitType"] == "ha"
    assert body["analysisOptions"]["externalIdColumn"] == "external_ref"
    assert body["features"][0]["properties"]["external_ref"] == "lot-7"


@respx.mock
def test_no_external_ref_omits_id_column(respx_mock: respx.MockRouter) -> None:
    _submit_ok(respx_mock)

    with _client() as client:
        client.analyze(POLYGON)

    body = respx_mock.calls[0].request.content
    import json

    options = json.loads(body)["analysisOptions"]
    assert "externalIdColumn" not in options


# --------------------------------------------------------------------------- #
# Failure modes — all fail loud                                                #
# --------------------------------------------------------------------------- #
def test_missing_key_raises_not_configured() -> None:
    # No HTTP is attempted when the key is empty.
    client = WhispClient(api_url=API_URL, api_key="", client=httpx.Client())
    with pytest.raises(RiskProviderNotConfigured, match="Whisp API key is required"):
        client.analyze(POLYGON)


@respx.mock
def test_invalid_key_raises_not_configured(respx_mock: respx.MockRouter) -> None:
    # A key that the server rejects is still a configuration problem.
    respx_mock.post(f"{API_URL}/submit/geojson").mock(
        return_value=httpx.Response(
            401,
            json={"code": "auth_invalid_api_key", "message": "Invalid or expired API key."},
        )
    )
    with _client() as client, pytest.raises(
        RiskProviderNotConfigured, match="auth_invalid_api_key"
    ):
        client.analyze(POLYGON)


@respx.mock
def test_server_error_raises_provider_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{API_URL}/submit/geojson").mock(
        return_value=httpx.Response(
            500,
            json={
                "code": "system_internal_server_error",
                "message": "An internal server error occurred. Please try again later.",
            },
        )
    )
    with _client() as client, pytest.raises(
        RiskProviderError, match="system_internal_server_error"
    ):
        client.analyze(POLYGON)


@respx.mock
def test_validation_error_raises_provider_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{API_URL}/submit/geojson").mock(
        return_value=httpx.Response(
            422,
            json={
                "code": "validation_invalid_geojson",
                "message": "The supplied GeoJSON is invalid.",
                "cause": "features[0].geometry",
            },
        )
    )
    with _client() as client, pytest.raises(
        RiskProviderError, match="validation_invalid_geojson.*cause"
    ):
        client.analyze(POLYGON)


@respx.mock
def test_completed_code_but_no_features_raises(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{API_URL}/submit/geojson").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "analysis_completed",
                "message": "ok",
                "data": {"type": "FeatureCollection", "features": []},
            },
        )
    )
    with _client() as client, pytest.raises(RiskProviderError, match="no features"):
        client.analyze(POLYGON)


@respx.mock
def test_completed_code_but_no_data_raises(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{API_URL}/submit/geojson").mock(
        return_value=httpx.Response(
            200, json={"code": "analysis_completed", "message": "ok"}
        )
    )
    with _client() as client, pytest.raises(RiskProviderError, match="no result data"):
        client.analyze(POLYGON)


@respx.mock
def test_non_envelope_body_raises_with_status(respx_mock: respx.MockRouter) -> None:
    # A proxy 502 with an HTML body has no envelope to quote.
    respx_mock.post(f"{API_URL}/submit/geojson").mock(
        return_value=httpx.Response(502, text="<html>Bad Gateway</html>")
    )
    with _client() as client, pytest.raises(RiskProviderError, match="HTTP 502"):
        client.analyze(POLYGON)


@respx.mock
def test_transport_error_raises_provider_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{API_URL}/submit/geojson").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    with _client() as client, pytest.raises(RiskProviderError, match="request failed"):
        client.analyze(POLYGON)


# --------------------------------------------------------------------------- #
# Module-level convenience                                                      #
# --------------------------------------------------------------------------- #
@respx.mock
def test_analyze_geometry_uses_settings(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # analyze_geometry builds its own client from settings; point it at the mock.
    monkeypatch.setattr("app.services.whisp.default_settings.whisp_api_url", API_URL)
    monkeypatch.setattr("app.services.whisp.default_settings.whisp_api_key", KEY)
    _submit_ok(respx_mock)

    evidence = analyze_geometry(POLYGON)
    assert evidence.provider == "whisp"
    assert evidence.raw == WHISP_PROPERTIES
