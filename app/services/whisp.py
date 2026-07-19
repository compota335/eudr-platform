"""Whisp API client: turns a plot geometry into ``PlotEvidence``.

Whisp (https://whisp.openforis.org/api, API v2.1.0, backed by the
``openforis-whisp`` library over Google Earth Engine) is the deforestation-data
provider behind Stage 4. This client uses its SYNCHRONOUS path: a single
``POST /submit/geojson`` with ``analysisOptions.async = false`` returns the
enriched GeoJSON directly in one response — no job token, no polling, no
separate result fetch. The sync path is documented to handle up to 250
geometries within a 60s budget (``GET /api/config``), which is ample for the
one-plot-at-a-time use case here.

RESPONSE ENVELOPE (verified against the live API, 2026-07). Every response —
success or error — shares one shape::

    {"code": <SystemCode>, "message": str, "cause": str | None, "data": <any>}

A successful analysis is HTTP 200 with ``code == "analysis_completed"`` and
``data`` a GeoJSON ``FeatureCollection`` whose first feature's ``properties``
holds the Whisp result columns (``EUFO_2020``, ``GFC_loss_after_2020``, ...)
consumed by :func:`evidence_from_whisp_properties`. Errors carry a non-2xx
status with the SAME envelope: ``401 auth_missing_api_key`` /
``401 auth_invalid_api_key``, ``4xx validation_*``, ``5xx`` system/analysis
codes.

The client fails loud (house rules). A missing or invalid/expired key raises
``RiskProviderNotConfigured`` (both are "fix your ``WHISP_API_KEY``" problems);
any transport error, a non-2xx status, a success body whose ``code`` is not
``analysis_completed``, a JSON decode error, or an unexpected payload shape
raises ``RiskProviderError`` naming what failed. It NEVER returns a fabricated
or empty ``PlotEvidence``.

Areas are requested in HECTARES (``analysisOptions.unitType = "ha"``) because
the risk engine's thresholds (``MIN_SIGNAL_HA``, ``MIN_FOREST_HA``) and every
``DatasetSignal.value`` are in hectares; requesting any other unit would
silently corrupt the verdict.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings
from app.config import settings as default_settings
from app.geo.schemas import (
    Geometry,
    PlotEvidence,
    RiskProviderError,
    RiskProviderNotConfigured,
)
from app.services.risk import evidence_from_whisp_properties

# API version recorded on the evidence for reproducibility (``GET`` openapi
# ``info.version``). Bump when the verified contract changes.
_API_VERSION = "v2.1.0"

# The only ``code`` a synchronous analysis returns on success.
_SUCCESS_CODE = "analysis_completed"

# Auth failures are a configuration problem, not a transient provider fault, so
# they map to ``RiskProviderNotConfigured`` (the operator must fix the key).
_AUTH_ERROR_CODES = frozenset({"auth_missing_api_key", "auth_invalid_api_key"})

# A hair above the documented 60s sync analysis budget so the SERVER times out
# and returns ``analysis_timeout`` (which we surface) before the client aborts.
_DEFAULT_TIMEOUT = 90.0


class WhispClient:
    """Synchronous client for the Whisp deforestation API (sync submit path).

    The whole codebase is synchronous and the calling endpoint runs this in a
    threadpool, so a blocking ``httpx.Client`` is the right tool. All network
    parameters (URL, key, timeout) are injectable for testability; nothing is
    hardcoded.
    """

    def __init__(
        self,
        *,
        api_url: str | None = None,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        config: Settings | None = None,
    ) -> None:
        """Build a client from ``settings`` with per-argument overrides.

        ``api_url`` / ``api_key`` fall back to ``config`` (the app settings).
        A caller may inject its own ``httpx.Client`` (respx routes it in tests);
        otherwise one is created with ``timeout`` as the per-request timeout.
        """
        cfg = config or default_settings
        self._api_url = (api_url if api_url is not None else cfg.whisp_api_url).rstrip("/")
        self._api_key = api_key if api_key is not None else cfg.whisp_api_key
        self._timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    # ----------------------------------------------------------------------- #
    # Public API                                                              #
    # ----------------------------------------------------------------------- #
    def analyze(
        self, geometry: Geometry, *, external_ref: str | None = None
    ) -> PlotEvidence:
        """Run one synchronous analysis for a single geometry.

        ``geometry`` is a single GeoJSON geometry dict (Point / Polygon /
        MultiPolygon, WGS84). Returns the ``PlotEvidence`` built from the Whisp
        result. Raises ``RiskProviderNotConfigured`` if no key is set (or the
        server rejects the key), or ``RiskProviderError`` naming the failure on
        any other error.
        """
        if not self._api_key:
            raise RiskProviderNotConfigured(
                "a Whisp API key is required; set WHISP_API_KEY to call the provider"
            )
        envelope = self._run_analysis(_build_payload(geometry, external_ref))
        properties = _first_feature_properties(envelope)
        return evidence_from_whisp_properties(
            properties, provider="whisp", dataset_versions={"whisp_api": _API_VERSION}
        )

    def close(self) -> None:
        """Close the underlying HTTP client if this instance created it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> WhispClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ----------------------------------------------------------------------- #
    # Transport                                                               #
    # ----------------------------------------------------------------------- #
    def _run_analysis(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST the geometry and return the success envelope, or fail loud.

        Both success and error responses are the shared ``{code, message, ...}``
        envelope; only ``HTTP 2xx`` with ``code == analysis_completed`` is a
        success. Every other outcome raises: auth codes become
        ``RiskProviderNotConfigured``, everything else ``RiskProviderError``
        carrying the server's own ``code`` / ``message`` / ``cause``.
        """
        url = f"{self._api_url}/submit/geojson"
        try:
            response = self._client.request(
                "POST",
                url,
                headers={"x-api-key": self._api_key},
                json=payload,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise RiskProviderError(f"Whisp submit request failed: {exc}") from exc

        envelope = _try_decode_envelope(response)
        code = envelope.get("code") if envelope is not None else None

        if response.is_success and code == _SUCCESS_CODE:
            return envelope  # type: ignore[return-value]  # guarded by code check
        if code in _AUTH_ERROR_CODES:
            raise RiskProviderNotConfigured(_envelope_message(envelope))
        if envelope is not None and code is not None:
            raise RiskProviderError(_envelope_message(envelope))
        raise RiskProviderError(
            f"Whisp submit returned HTTP {response.status_code} with an "
            f"unrecognized body for {url}"
        )


# --------------------------------------------------------------------------- #
# Module-level convenience                                                      #
# --------------------------------------------------------------------------- #
def analyze_geometry(
    geometry: Geometry, *, external_ref: str | None = None
) -> PlotEvidence:
    """Analyze one geometry with a client built from application settings.

    Convenience wrapper around :class:`WhispClient` for callers that do not
    need to reuse a client. Raises the same errors as ``WhispClient.analyze``.
    """
    with WhispClient() as client:
        return client.analyze(geometry, external_ref=external_ref)


# --------------------------------------------------------------------------- #
# Internal helpers                                                              #
# --------------------------------------------------------------------------- #
def _build_payload(geometry: Geometry, external_ref: str | None) -> dict[str, Any]:
    """Wrap one geometry as the sync submit body Whisp expects.

    A single-feature ``FeatureCollection`` plus ``analysisOptions`` pinning the
    synchronous path and hectare units. When ``external_ref`` is given it rides
    along in the feature's ``properties`` and ``externalIdColumn`` names that
    property so Whisp echoes it back on the result row.
    """
    feature: dict[str, Any] = {
        "type": "Feature",
        "geometry": geometry,
        "properties": {} if external_ref is None else {"external_ref": external_ref},
    }
    options: dict[str, Any] = {"async": False, "unitType": "ha"}
    if external_ref is not None:
        options["externalIdColumn"] = "external_ref"
    return {
        "type": "FeatureCollection",
        "features": [feature],
        "analysisOptions": options,
    }


def _try_decode_envelope(response: httpx.Response) -> dict[str, Any] | None:
    """Return the response JSON if it is an object, else ``None``.

    Used for both success and error responses (they share the envelope). A
    non-JSON or non-object body (a proxy's HTML 502, say) yields ``None`` so the
    caller can fall back to a status-code-only error.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _envelope_message(envelope: dict[str, Any] | None) -> str:
    """Render the server envelope as a single fail-loud message string."""
    if not envelope:
        return "Whisp returned an empty response"
    detail = f"Whisp: {envelope.get('code', 'unknown')}"
    message = envelope.get("message")
    if message:
        detail += f" - {message}"
    cause = envelope.get("cause")
    if cause:
        detail += f" (cause: {cause})"
    return detail


def _first_feature_properties(envelope: dict[str, Any]) -> dict[str, Any]:
    """Pull the first result feature's ``properties`` out of a success envelope.

    Raises ``RiskProviderError`` if ``data`` is not a FeatureCollection, holds
    no features, or the first feature has no ``properties`` object — the
    contract changed and we must not guess a value into existence.
    """
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise RiskProviderError(
            "Whisp analysis completed but carried no result data object"
        )
    features = data.get("features")
    if not isinstance(features, list) or not features:
        raise RiskProviderError("Whisp analysis result contained no features")
    first = features[0]
    properties = first.get("properties") if isinstance(first, dict) else None
    if not isinstance(properties, dict) or not properties:
        raise RiskProviderError(
            "Whisp analysis result feature had no properties object"
        )
    return properties
