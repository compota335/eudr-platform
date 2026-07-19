"""Whisp async-job API client: turns a plot geometry into ``PlotEvidence``.

Whisp (https://whisp.openforis.org/api, v2.1.0) is the deforestation-data
provider behind Stage 4. It is an ASYNCHRONOUS JOB API: you submit a geometry,
receive a job token, poll the job until it reaches a terminal state, then fetch
the enriched GeoJSON result. This client walks that three-step handshake and
hands the first result feature's ``properties`` to
``evidence_from_whisp_properties`` so the risk engine never sees Whisp-specific
column names.

The client fails loud (see the house rules). A missing API key, any transport
error, a non-2xx status, a JSON decode error, an unexpected payload shape, a
job that reports failure, or a poll timeout all raise ``RiskProviderError`` (or
its subclass ``RiskProviderNotConfigured``) naming the step that failed. It
NEVER returns a fabricated or empty ``PlotEvidence`` on failure.

ASSUMED RESPONSE SHAPES (must be confirmed against the live API once a key is
obtained; they could not be captured live because we hold no key):

* ``POST {api}/submit/geojson`` -> JSON carrying the job token as ``token`` at
  the top level, or nested as ``data.token``. Both shapes are accepted.
* ``GET {api}/status/{token}`` -> JSON carrying the job state as ``status`` at
  the top level, or nested as ``data.status``. Terminal-success values are
  ``completed`` / ``success`` / ``finished`` / ``done`` (case-insensitive);
  terminal-failure values are ``failed`` / ``error``. Anything else is treated
  as "still running" and polling continues until the attempt budget is spent.
* ``GET {api}/generate-geojson/{token}`` -> a GeoJSON ``FeatureCollection``;
  the FIRST feature's ``properties`` object holds the Whisp result columns
  (e.g. ``GFC_loss_after_2020``) consumed by ``evidence_from_whisp_properties``.

Parsing is defensive but never invents values: if none of the accepted shapes
match, the client raises rather than guessing a default into existence.
"""

from __future__ import annotations

import time
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

# Job-status strings (lower-cased before comparison) that end the poll loop.
_SUCCESS_STATES = frozenset({"completed", "success", "finished", "done"})
_FAILURE_STATES = frozenset({"failed", "error"})

# Defaults sized to the documented ~600s async cap: 150 polls * 2.0s ~ 300s of
# waiting plus request time, with the total also bounded by ``max_wait``.
_DEFAULT_POLL_INTERVAL = 2.0
_DEFAULT_MAX_ATTEMPTS = 150
_DEFAULT_MAX_WAIT = 600.0
_DEFAULT_TIMEOUT = 30.0


class WhispClient:
    """Synchronous client for the Whisp async-job deforestation API.

    The whole codebase is synchronous and the calling endpoint runs this in a
    threadpool, so a blocking ``httpx.Client`` is the right tool. All network
    parameters (URL, key, timeout, poll cadence) are injectable for testability;
    nothing is hardcoded.
    """

    def __init__(
        self,
        *,
        api_url: str | None = None,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        max_wait: float = _DEFAULT_MAX_WAIT,
        config: Settings | None = None,
    ) -> None:
        """Build a client from ``settings`` with per-argument overrides.

        ``api_url`` / ``api_key`` fall back to ``config`` (the app settings).
        A caller may inject its own ``httpx.Client`` (respx routes it in tests);
        otherwise one is created with ``timeout`` as the per-request timeout.
        ``poll_interval`` / ``max_attempts`` / ``max_wait`` bound the poll loop.
        """
        cfg = config or default_settings
        self._api_url = (api_url if api_url is not None else cfg.whisp_api_url).rstrip("/")
        self._api_key = api_key if api_key is not None else cfg.whisp_api_key
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._max_attempts = max_attempts
        self._max_wait = max_wait
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    # ----------------------------------------------------------------------- #
    # Public API                                                              #
    # ----------------------------------------------------------------------- #
    def analyze(
        self, geometry: Geometry, *, external_ref: str | None = None
    ) -> PlotEvidence:
        """Run the full submit -> poll -> fetch handshake for one geometry.

        ``geometry`` is a single GeoJSON geometry dict (Point / Polygon /
        MultiPolygon, WGS84). Returns the ``PlotEvidence`` built from the Whisp
        result. Raises ``RiskProviderNotConfigured`` if no key is set, or
        ``RiskProviderError`` naming the failing step on any other failure.
        """
        if not self._api_key:
            raise RiskProviderNotConfigured(
                "a Whisp API key is required; set WHISP_API_KEY to call the provider"
            )
        token = self._submit(geometry, external_ref=external_ref)
        self._poll_until_done(token)
        properties = self._fetch_result_properties(token)
        return evidence_from_whisp_properties(
            properties, provider="whisp", dataset_versions={"whisp_api": "v2.1.0"}
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
    # Handshake steps                                                         #
    # ----------------------------------------------------------------------- #
    def _submit(self, geometry: Geometry, *, external_ref: str | None) -> str:
        """POST the geometry and return the async job token."""
        feature: dict[str, Any] = {
            "type": "Feature",
            "geometry": geometry,
            "properties": {} if external_ref is None else {"external_ref": external_ref},
        }
        payload = {"type": "FeatureCollection", "features": [feature]}
        data = self._request_json(
            "POST",
            f"{self._api_url}/submit/geojson",
            step="submit",
            json=payload,
        )
        token = _extract(data, "token")
        if not isinstance(token, str) or not token:
            raise RiskProviderError(
                "Whisp submit did not return a usable job token; "
                f"received keys {sorted(data)!r}"
            )
        return token

    def _poll_until_done(self, token: str) -> None:
        """Poll the job status until it succeeds, fails, or the budget runs out."""
        started = time.monotonic()
        for _attempt in range(self._max_attempts):
            data = self._request_json(
                "GET",
                f"{self._api_url}/status/{token}",
                step="status",
            )
            raw_status = _extract(data, "status")
            if not isinstance(raw_status, str) or not raw_status:
                raise RiskProviderError(
                    "Whisp status did not return a status string; "
                    f"received keys {sorted(data)!r}"
                )
            status = raw_status.strip().lower()
            if status in _SUCCESS_STATES:
                return
            if status in _FAILURE_STATES:
                raise RiskProviderError(
                    f"Whisp job {token} reported failure with status {raw_status!r}"
                )
            if time.monotonic() - started >= self._max_wait:
                break
            if self._poll_interval > 0:
                time.sleep(self._poll_interval)
        raise RiskProviderError(
            f"Whisp job {token} did not finish within {self._max_attempts} polls "
            f"/ {self._max_wait:.0f}s"
        )

    def _fetch_result_properties(self, token: str) -> dict[str, Any]:
        """Fetch the result FeatureCollection and return the first feature's props."""
        data = self._request_json(
            "GET",
            f"{self._api_url}/generate-geojson/{token}",
            step="generate-geojson",
        )
        features = data.get("features")
        if not isinstance(features, list) or not features:
            raise RiskProviderError(
                f"Whisp generate-geojson for job {token} returned no features"
            )
        first = features[0]
        properties = first.get("properties") if isinstance(first, dict) else None
        if not isinstance(properties, dict) or not properties:
            raise RiskProviderError(
                f"Whisp generate-geojson for job {token} returned a feature "
                "without a properties object"
            )
        return properties

    # ----------------------------------------------------------------------- #
    # Transport                                                               #
    # ----------------------------------------------------------------------- #
    def _request_json(
        self, method: str, url: str, *, step: str, json: Any | None = None
    ) -> dict[str, Any]:
        """Issue one request and decode its JSON object, failing loud per step.

        Wraps every transport-, status-, and decode-level failure into
        ``RiskProviderError`` tagged with the pipeline ``step`` that failed.
        """
        headers = {"X-API-KEY": self._api_key}
        try:
            response = self._client.request(
                method, url, headers=headers, json=json, timeout=self._timeout
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RiskProviderError(
                f"Whisp {step} returned HTTP {exc.response.status_code} for {url}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RiskProviderError(
                f"Whisp {step} request failed: {exc}"
            ) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise RiskProviderError(
                f"Whisp {step} returned a non-JSON body for {url}"
            ) from exc
        if not isinstance(body, dict):
            raise RiskProviderError(
                f"Whisp {step} returned a JSON {type(body).__name__}, expected an object"
            )
        return body


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
def _extract(data: dict[str, Any], key: str) -> Any:
    """Return ``data[key]`` if present, else ``data['data'][key]``, else None.

    Accepts both the flat and the ``data``-wrapped response shapes documented in
    the module docstring without inventing a value when neither is present.
    """
    if key in data:
        return data[key]
    nested = data.get("data")
    if isinstance(nested, dict) and key in nested:
        return nested[key]
    return None
