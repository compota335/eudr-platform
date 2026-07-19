"""Geometric and agronomic validation of a parsed plot.

This is the ``validate`` stage of the plot-checker pipeline: it takes a
:class:`~app.geo.schemas.NormalizedPlot` (geometry as read from user input,
already in WGS84 lon/lat order) and returns a
:class:`~app.geo.schemas.ValidatedPlot` carrying derived metrics, or raises
:class:`~app.geo.schemas.GeoValidationError` if a geometric check fails.

The module fails loud. It never repairs invalid geometry: a self-intersecting
polygon is rejected with the reason ``shapely`` reports, not silently patched
with ``buffer(0)``. Winding is the only thing normalised, because RFC 7946
mandates a specific ring orientation and reordering rings does not change the
shape. Agronomic checks (the 4 ha polygon rule and yield plausibility) are
advisory: they add warnings, they never reject.
"""

from __future__ import annotations

from typing import Any

from pyproj import Geod
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.validation import explain_validity

from app.geo.schemas import (
    Geometry,
    GeoValidationError,
    NormalizedPlot,
    ValidatedPlot,
)
from app.models.enums import Commodity

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #
_SUPPORTED_TYPES = frozenset({"Point", "Polygon", "MultiPolygon"})
_AREAL_TYPES = frozenset({"Polygon", "MultiPolygon"})

# Geodesic area is computed on the WGS84 ellipsoid so hectares are physically
# meaningful regardless of latitude (a planar area on lon/lat would be wrong).
_GEOD = Geod(ellps="WGS84")

# Agronomic sanity bands, expressed as commodity yield in tonnes per hectare.
# These are deliberately WIDE plausibility bounds, not agronomic targets: their
# only job is to catch unit mistakes and gross data-entry errors (e.g. volume
# in kg entered as tonnes, or an area off by a factor of a hundred). A value
# outside the band is a warning, never a rejection.
#
# ``cattle`` is intentionally absent: pasture is measured by stocking rate
# (head per hectare), not by a tonnes-per-hectare yield, so no band applies.
#
# Versioned so a warning can be traced back to the exact table that produced
# it; bump this string whenever a band changes.
YIELD_BANDS_VERSION = "2024.1"
YIELD_BANDS: dict[Commodity, tuple[float, float]] = {
    Commodity.coffee: (0.3, 3.0),
    Commodity.cocoa: (0.2, 2.0),
    Commodity.oil_palm: (8.0, 35.0),
    Commodity.rubber: (0.5, 3.0),
    Commodity.soya: (1.0, 4.5),
    Commodity.wood: (1.0, 30.0),
}

# Midpoint yields (t/ha) used only to estimate the implied area of a Point plot
# for the EUDR 4 ha polygon rule. Derived from ``YIELD_BANDS`` so the two tables
# cannot drift apart.
_MIDPOINT_YIELD: dict[Commodity, float] = {
    commodity: (lo + hi) / 2.0 for commodity, (lo, hi) in YIELD_BANDS.items()
}

# EUDR requires a polygon (not a point) for a plot of this size or larger.
_POLYGON_REQUIRED_HA = 4.0


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #
def validate_plot(
    plot: NormalizedPlot,
    *,
    declared_country: str | None = None,
    commodity: Commodity | None = None,
    declared_volume_t: float | None = None,
) -> ValidatedPlot:
    """Validate one parsed plot and derive its metrics.

    Runs the geometric checks (non-empty, supported type, in-range coordinates,
    topological validity), normalises ring winding to RFC 7946, then computes
    geodesic area and centroid. Finally it appends any advisory warnings (the
    4 ha polygon rule and yield plausibility).

    ``declared_country`` is accepted for signature stability with the rest of
    the pipeline; it does not affect the geometric result and is not consulted
    here.

    Raises:
        GeoValidationError: the geometry is empty, of an unsupported type, has a
            coordinate out of range, or is topologically invalid.
    """
    geom = shape(plot.geometry)
    if geom.is_empty:
        raise GeoValidationError("geometry is empty")

    geom_type = geom.geom_type
    if geom_type not in _SUPPORTED_TYPES:
        raise GeoValidationError(
            f"unsupported geometry type: {geom_type} "
            f"(expected one of {sorted(_SUPPORTED_TYPES)})"
        )

    _check_coordinate_ranges(plot.geometry)

    if geom_type in _AREAL_TYPES and not geom.is_valid:
        raise GeoValidationError(
            f"invalid {geom_type}: {explain_validity(geom)}"
        )

    rewound = fix_winding(plot.geometry)
    oriented = shape(rewound)

    area_ha = round(geodesic_area_ha(rewound), 6)
    centroid_lon, centroid_lat = _centroid(oriented)

    warnings = _build_warnings(
        geometry_type=geom_type,
        area_ha=area_ha,
        commodity=commodity,
        declared_volume_t=declared_volume_t,
    )

    return ValidatedPlot(
        geometry=rewound,
        geometry_type=geom_type,
        area_ha=area_ha,
        centroid_lon=centroid_lon,
        centroid_lat=centroid_lat,
        source_format=plot.source_format,
        external_ref=plot.external_ref,
        warnings=warnings,
    )


def geodesic_area_ha(geometry: Geometry) -> float:
    """Return the geodesic area of ``geometry`` in hectares (WGS84 ellipsoid).

    A ``Point`` has no area and returns ``0.0``. Areas are always non-negative:
    the sign reported by ``pyproj`` only encodes ring orientation, so it is
    discarded.
    """
    geom = shape(geometry)
    if geom.geom_type == "Point":
        return 0.0
    area_m2, _perimeter = _GEOD.geometry_area_perimeter(geom)
    return abs(area_m2) / 10_000.0


def fix_winding(geometry: Geometry) -> Geometry:
    """Normalise polygon ring winding to RFC 7946 and return a GeoJSON dict.

    RFC 7946 requires exterior rings counter-clockwise and interior rings
    (holes) clockwise. ``Polygon`` and ``MultiPolygon`` are re-oriented with
    ``shapely.geometry.polygon.orient(..., sign=1.0)``; a ``Point`` has no
    rings and is returned unchanged. Reordering rings never changes the shape,
    so this is safe to apply before measuring.
    """
    geom = shape(geometry)
    geom_type = geom.geom_type
    if geom_type == "Point":
        return mapping(geom)
    if geom_type == "Polygon":
        return mapping(orient(geom, sign=1.0))
    if geom_type == "MultiPolygon":
        oriented = [orient(part, sign=1.0) for part in geom.geoms]
        return mapping(type(geom)(oriented))
    raise GeoValidationError(
        f"cannot fix winding for geometry type: {geom_type}"
    )


# --------------------------------------------------------------------------- #
# Internals                                                                    #
# --------------------------------------------------------------------------- #
def _check_coordinate_ranges(geometry: Geometry) -> None:
    """Raise ``GeoValidationError`` if any coordinate is out of WGS84 range.

    Walks the raw GeoJSON ``coordinates`` (not the shapely object) so the
    offending longitude/latitude can be named exactly in the error.
    """
    for lon, lat in _iter_coords(geometry.get("coordinates")):
        if not -180.0 <= lon <= 180.0:
            raise GeoValidationError(
                f"longitude out of range [-180, 180]: {lon}"
            )
        if not -90.0 <= lat <= 90.0:
            raise GeoValidationError(
                f"latitude out of range [-90, 90]: {lat}"
            )


def _iter_coords(coordinates: Any) -> Any:
    """Yield ``(lon, lat)`` pairs from an arbitrarily nested coordinate array.

    GeoJSON positions may carry a third element (altitude); only the first two
    values are yielded.
    """
    if coordinates is None:
        return
    # A position is a flat list whose first element is a number.
    if coordinates and isinstance(coordinates[0], (int, float)):
        yield float(coordinates[0]), float(coordinates[1])
        return
    for item in coordinates:
        yield from _iter_coords(item)


def _centroid(geom: BaseGeometry) -> tuple[float, float]:
    """Return the ``(lon, lat)`` representative point of ``geom``.

    For a ``Point`` this is its own coordinates; for areal geometries it is the
    shapely centroid.
    """
    if geom.geom_type == "Point":
        return float(geom.x), float(geom.y)
    centroid = geom.centroid
    return float(centroid.x), float(centroid.y)


def _build_warnings(
    *,
    geometry_type: str,
    area_ha: float,
    commodity: Commodity | None,
    declared_volume_t: float | None,
) -> tuple[str, ...]:
    """Assemble the advisory (non-fatal) warnings for a validated plot."""
    warnings: list[str] = []

    # EUDR 4 ha rule: a Point stands in for a smallholder plot below 4 ha. If
    # the declared volume implies the plot is actually >= 4 ha, EUDR wants a
    # polygon instead. This only warns; the point is still accepted.
    if (
        geometry_type == "Point"
        and commodity is not None
        and declared_volume_t is not None
        and commodity in _MIDPOINT_YIELD
    ):
        implied_area_ha = declared_volume_t / _MIDPOINT_YIELD[commodity]
        if implied_area_ha >= _POLYGON_REQUIRED_HA:
            warnings.append(
                f"point geometry implies ~{implied_area_ha:.1f} ha "
                f"(>= {_POLYGON_REQUIRED_HA:.0f} ha); EUDR requires a polygon "
                f"for plots this size"
            )

    # Yield plausibility: catch unit/data-entry blunders on areal plots.
    if (
        commodity in YIELD_BANDS
        and declared_volume_t is not None
        and area_ha > 0.0
    ):
        lo, hi = YIELD_BANDS[commodity]
        implied_yield = declared_volume_t / area_ha
        if not lo <= implied_yield <= hi:
            warnings.append(
                f"implausible yield: {implied_yield:.2f} t/ha for "
                f"{commodity.value} (expected {lo}-{hi} t/ha)"
            )

    return tuple(warnings)
