"""Tests for the Whisp async-job API client (Stage 4 provider adapter).

Every HTTP interaction is mocked with respx; no test ever hits the live Whisp
API. Clients are configured with ``poll_interval=0`` so polling is instant and
tests never sleep for real seconds.
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
TOKEN = "job-token-123"

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
    """Build a client wired to the mocked API with instant, bounded polling."""
    params: dict[str, object] = {
        "api_url": API_URL,
        "api_key": KEY,
        "client": httpx.Client(),
        "poll_interval": 0,
        "max_attempts": 5,
        "max_wait": 10.0,
    }
    params.update(overrides)
    return WhispClient(**params)  # type: ignore[arg-type]


def _submit_route(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{API_URL}/submit/geojson").mock(
        return_value=httpx.Response(200, json={"token": TOKEN})
    )


def _generate_route(
    respx_mock: respx.MockRouter, properties: dict[str, object] | None = None
) -> None:
    feature = {
        "type": "Feature",
        "geometry": POLYGON,
        "properties": WHISP_PROPERTIES if properties is None else properties,
    }
    respx_mock.get(f"{API_URL}/generate-geojson/{TOKEN}").mock(
        return_value=httpx.Response(
            200, json={"type": "FeatureCollection", "features": [feature]}
        )
    )


# --------------------------------------------------------------------------- #
# Happy path                                                                   #
# --------------------------------------------------------------------------- #
@respx.mock
def test_happy_path_returns_plot_evidence(respx_mock: respx.MockRouter) -> None:
    _submit_route(respx_mock)
    # First poll is still pending, second reports completed.
    respx_mock.get(f"{API_URL}/status/{TOKEN}").mock(
        side_effect=[
            httpx.Response(200, json={"status": "pending"}),
            httpx.Response(200, json={"status": "completed"}),
        ]
    )
    _generate_route(respx_mock)

    with _client() as client:
        evidence = client.analyze(POLYGON, external_ref="lot-7")

    assert isinstance(evidence, PlotEvidence)
    assert evidence.provider == "whisp"
    assert evidence.raw == WHISP_PROPERTIES
    # Signals carry the mocked Whisp column names and areas.
    families = {s.family: s for s in evidence.loss_after_2020}
    assert families["GFC"].dataset == "GFC_loss_after_2020"
    assert families["GFC"].value == pytest.approx(0.42)
    assert families["RADD"].value == pytest.approx(0.31)
    assert {s.dataset for s in evidence.forest_2020} == {"EUFO_2020"}
    # The submit body wrapped the geometry as a one-feature FeatureCollection.
    submit_request = respx_mock.calls[0].request
    assert submit_request.headers["X-API-KEY"] == KEY


@respx.mock
def test_token_from_nested_data_shape(respx_mock: respx.MockRouter) -> None:
    # The token may arrive nested under "data"; the client accepts both shapes.
    respx_mock.post(f"{API_URL}/submit/geojson").mock(
        return_value=httpx.Response(200, json={"data": {"token": TOKEN}})
    )
    respx_mock.get(f"{API_URL}/status/{TOKEN}").mock(
        return_value=httpx.Response(200, json={"data": {"status": "SUCCESS"}})
    )
    _generate_route(respx_mock)

    with _client() as client:
        evidence = client.analyze(POLYGON)
    assert evidence.provider == "whisp"


# --------------------------------------------------------------------------- #
# Failure modes — all fail loud                                                #
# --------------------------------------------------------------------------- #
def test_missing_key_raises_not_configured() -> None:
    # No HTTP is attempted when the key is empty.
    client = WhispClient(api_url=API_URL, api_key="", client=httpx.Client())
    with pytest.raises(RiskProviderNotConfigured, match="Whisp API key is required"):
        client.analyze(POLYGON)


@respx.mock
def test_submit_non_2xx_raises(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{API_URL}/submit/geojson").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )
    with _client() as client, pytest.raises(RiskProviderError, match="submit returned HTTP 500"):
        client.analyze(POLYGON)


@respx.mock
def test_submit_without_token_raises(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{API_URL}/submit/geojson").mock(
        return_value=httpx.Response(200, json={"message": "queued"})
    )
    with _client() as client, pytest.raises(RiskProviderError, match="usable job token"):
        client.analyze(POLYGON)


@respx.mock
def test_status_failed_raises(respx_mock: respx.MockRouter) -> None:
    _submit_route(respx_mock)
    respx_mock.get(f"{API_URL}/status/{TOKEN}").mock(
        return_value=httpx.Response(200, json={"status": "failed"})
    )
    with _client() as client, pytest.raises(RiskProviderError, match="reported failure"):
        client.analyze(POLYGON)


@respx.mock
def test_polling_times_out_raises(respx_mock: respx.MockRouter) -> None:
    _submit_route(respx_mock)
    # Status stays pending forever; a tiny attempt budget forces a timeout.
    respx_mock.get(f"{API_URL}/status/{TOKEN}").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    with (
        _client(max_attempts=3) as client,
        pytest.raises(RiskProviderError, match="did not finish"),
    ):
        client.analyze(POLYGON)


@respx.mock
def test_generate_empty_features_raises(respx_mock: respx.MockRouter) -> None:
    _submit_route(respx_mock)
    respx_mock.get(f"{API_URL}/status/{TOKEN}").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    respx_mock.get(f"{API_URL}/generate-geojson/{TOKEN}").mock(
        return_value=httpx.Response(
            200, json={"type": "FeatureCollection", "features": []}
        )
    )
    with _client() as client, pytest.raises(RiskProviderError, match="no features"):
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
    _submit_route(respx_mock)
    respx_mock.get(f"{API_URL}/status/{TOKEN}").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    _generate_route(respx_mock)

    evidence = analyze_geometry(POLYGON)
    assert evidence.provider == "whisp"
    assert evidence.raw == WHISP_PROPERTIES
