"""Shared value objects and errors for the plot-checker pipeline.

These are the contracts every plot-checker module agrees on:

    parse   -> NormalizedPlot   (geometry as read from user input, WGS84)
    validate-> ValidatedPlot    (geometry that passed the geometric checks)
    provider-> PlotEvidence     (deforestation signals, provider-agnostic)
    risk    -> RiskResult       (deterministic green/amber/red verdict)

Everything here is a plain frozen dataclass with no I/O, so it is trivially
constructed in tests without touching the database or any external API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.enums import RiskLevel

# GeoJSON geometry object, e.g. {"type": "Polygon", "coordinates": [...]}.
Geometry = dict[str, Any]


# --------------------------------------------------------------------------- #
# Errors — the pipeline fails loud. No silent skips, no fabricated results.    #
# --------------------------------------------------------------------------- #
class GeoError(ValueError):
    """Base class for all recoverable plot-checker input errors."""


class GeoParseError(GeoError):
    """Input could not be parsed into a geometry (bad/empty/unsupported)."""


class GeoValidationError(GeoError):
    """A geometry parsed but failed a geometric or plausibility check."""


class RiskProviderError(RuntimeError):
    """A deforestation-data provider call failed (I/O, timeout, bad reply)."""


class RiskProviderNotConfigured(RiskProviderError):
    """The provider has no credentials configured; it cannot be called."""


# --------------------------------------------------------------------------- #
# Geometry value objects                                                       #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NormalizedPlot:
    """One geometry parsed from user input and reprojected to WGS84.

    Not yet validated: the coordinates are in EPSG:4326 order (lon, lat) but
    winding, self-intersection and area have not been checked.
    """

    geometry: Geometry
    source_format: str  # geojson | csv | wkt | kml | kmz | shapefile
    external_ref: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidatedPlot:
    """A geometry that passed validation, with derived metrics."""

    geometry: Geometry
    geometry_type: str  # Point | Polygon | MultiPolygon
    area_ha: float  # geodesic area; 0.0 for a Point
    centroid_lon: float
    centroid_lat: float
    source_format: str
    external_ref: str | None = None
    warnings: tuple[str, ...] = ()  # non-fatal notes (e.g. yield implausibility)


# --------------------------------------------------------------------------- #
# Deforestation evidence — normalized, provider-agnostic                       #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DatasetSignal:
    """One dataset's contribution to the convergence-of-evidence assessment.

    ``family`` groups datasets that are NOT independent (e.g. GLAD-Landsat and
    GLAD-Sentinel-2 are both ``GLAD``); the risk engine counts distinct
    families, so two views of the same sensor never inflate "convergence".
    """

    dataset: str  # concrete column, e.g. "GFC_loss_after_2020"
    family: str  # independence key, e.g. "GFC" | "TMF" | "RADD" | "GLAD"
    value: float  # area (ha) of the dataset inside the plot
    kind: str  # forest_2020 | loss_after_2020 | commodity_2020


@dataclass(frozen=True)
class PlotEvidence:
    """Normalized deforestation signals for a single plot.

    Built by a provider adapter (Whisp today; GFW or local rasters later) so
    the risk engine never sees provider-specific column names.
    """

    forest_2020: tuple[DatasetSignal, ...] = ()
    loss_after_2020: tuple[DatasetSignal, ...] = ()
    loss_after_2020_buffer: tuple[DatasetSignal, ...] = ()
    commodity_2020: tuple[DatasetSignal, ...] = ()
    provider: str = ""
    dataset_versions: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)  # provider payload, for audit


# --------------------------------------------------------------------------- #
# Risk verdict                                                                 #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RiskResult:
    """The deterministic deforestation verdict for one plot.

    ``ruleset_version`` pins the exact rule table that produced ``level`` so a
    verdict can always be reproduced and re-explained years later.
    """

    level: RiskLevel
    ruleset_version: str
    forest_2020_present: bool
    signals_in_plot: int
    signals_in_buffer: int
    converging_families: tuple[str, ...]
    rationale: tuple[str, ...]  # one line per driver, each citing a signal
    evidence: PlotEvidence
