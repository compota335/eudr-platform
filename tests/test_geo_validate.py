"""Tests for the geometric and agronomic plot validation stage."""

from __future__ import annotations

import math

import pytest

from app.geo.schemas import GeoValidationError, NormalizedPlot, ValidatedPlot
from app.geo.validate import (
    fix_winding,
    geodesic_area_ha,
    validate_plot,
)
from app.models.enums import Commodity

# A ~1 ha square near the equator. 0.0009013 deg ~= 100 m at the equator, so
# the box is ~100 m x ~100 m ~= 1.0 ha. We still assert within 5% of the value
# computed independently by ``geodesic_area_ha`` rather than a hand constant.
_SIDE_DEG = 0.0009013
# CCW exterior square (RFC 7946 compliant).
_SQUARE_CCW = {
    "type": "Polygon",
    "coordinates": [
        [
            [0.0, 0.0],
            [_SIDE_DEG, 0.0],
            [_SIDE_DEG, _SIDE_DEG],
            [0.0, _SIDE_DEG],
            [0.0, 0.0],
        ]
    ],
}
# Same square wound clockwise; validation must normalise it to CCW.
_SQUARE_CW = {
    "type": "Polygon",
    "coordinates": [
        [
            [0.0, 0.0],
            [0.0, _SIDE_DEG],
            [_SIDE_DEG, _SIDE_DEG],
            [_SIDE_DEG, 0.0],
            [0.0, 0.0],
        ]
    ],
}


def _plot(geometry: dict, **kw: object) -> NormalizedPlot:
    return NormalizedPlot(geometry=geometry, source_format="geojson", **kw)


def _is_ccw(polygon_geojson: dict) -> bool:
    """Signed shoelace area > 0 means the ring is counter-clockwise."""
    ring = polygon_geojson["coordinates"][0]
    total = 0.0
    # Consecutive vertex pairs of a closed ring (last == first), so the two
    # sliced sequences deliberately differ in length by one.
    for (x1, y1), (x2, y2) in zip(ring, ring[1:], strict=False):
        total += (x2 - x1) * (y2 + y1)
    # Shoelace with this edge form: negative sum => CCW.
    return total < 0.0


def test_square_polygon_area_is_about_one_hectare() -> None:
    result = validate_plot(_plot(_SQUARE_CCW))
    assert isinstance(result, ValidatedPlot)
    assert result.geometry_type == "Polygon"
    # ~1.05 ha for this box; assert we are within 5% of the independent geodesic
    # computation and in the right ballpark (0.9-1.2 ha).
    expected = geodesic_area_ha(_SQUARE_CCW)
    assert 0.9 <= result.area_ha <= 1.2
    assert abs(result.area_ha - expected) <= 0.05 * expected
    # Centroid sits at the box centre.
    assert result.centroid_lon == pytest.approx(_SIDE_DEG / 2, abs=1e-6)
    assert result.centroid_lat == pytest.approx(_SIDE_DEG / 2, abs=1e-6)
    assert result.source_format == "geojson"


def test_winding_is_normalised_to_ccw() -> None:
    # Sanity: the input really is clockwise.
    assert not _is_ccw(_SQUARE_CW)
    rewound = fix_winding(_SQUARE_CW)
    assert _is_ccw(rewound)
    # And validate_plot returns the rewound geometry, not the input.
    result = validate_plot(_plot(_SQUARE_CW))
    assert _is_ccw(result.geometry)


def test_self_intersecting_bowtie_raises() -> None:
    bowtie = {
        "type": "Polygon",
        "coordinates": [
            [[0.0, 0.0], [1.0, 1.0], [1.0, 0.0], [0.0, 1.0], [0.0, 0.0]]
        ],
    }
    with pytest.raises(GeoValidationError, match="Self-intersection"):
        validate_plot(_plot(bowtie))


def test_out_of_range_coordinate_raises() -> None:
    bad = {"type": "Point", "coordinates": [200.0, 10.0]}
    with pytest.raises(GeoValidationError, match="longitude out of range"):
        validate_plot(_plot(bad))

    bad_lat = {"type": "Point", "coordinates": [10.0, 95.0]}
    with pytest.raises(GeoValidationError, match="latitude out of range"):
        validate_plot(_plot(bad_lat))


def test_empty_geometry_raises() -> None:
    empty = {"type": "Polygon", "coordinates": []}
    with pytest.raises(GeoValidationError, match="empty"):
        validate_plot(_plot(empty))


def test_point_has_zero_area() -> None:
    point = {"type": "Point", "coordinates": [-46.6, -23.5]}
    result = validate_plot(_plot(point))
    assert result.geometry_type == "Point"
    assert result.area_ha == 0.0
    assert result.centroid_lon == pytest.approx(-46.6)
    assert result.centroid_lat == pytest.approx(-23.5)
    assert geodesic_area_ha(point) == 0.0


def test_plausible_coffee_yield_has_no_warning() -> None:
    # A ~2 ha polygon with 3 t declared => 1.5 t/ha, inside coffee's band.
    result = validate_plot(
        _plot(_TWO_HA_SQUARE),
        commodity=Commodity.coffee,
        declared_volume_t=3.0,
    )
    assert result.area_ha == pytest.approx(2.0, rel=0.05)
    assert result.warnings == ()


def test_implausible_coffee_yield_produces_warning() -> None:
    # Same ~2 ha polygon with 400 t declared => ~200 t/ha, far above the band.
    result = validate_plot(
        _plot(_TWO_HA_SQUARE),
        commodity=Commodity.coffee,
        declared_volume_t=400.0,
    )
    assert any("implausible yield" in w for w in result.warnings)
    assert any("coffee" in w for w in result.warnings)


def test_geodesic_area_of_one_degree_box_is_sane() -> None:
    # A 1 deg x 1 deg box on the equator is ~12,300 km^2 == ~1.23 million ha.
    box = {
        "type": "Polygon",
        "coordinates": [
            [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]
        ],
    }
    area = geodesic_area_ha(box)
    # ~1.23e6 ha; assert the right order of magnitude and sign.
    assert area > 0.0
    assert math.isclose(area, 1.23e6, rel_tol=0.05)


# A ~2 ha square: a box with this side near the equator measures ~2.0 ha.
# side ~= 0.0012747 deg => ~142 m. The plausible-yield test asserts the area is
# within 5% of 2 ha, so the constant is pinned to geodesic_area_ha's own result.
_TWO_HA_SIDE = 0.0012747
_TWO_HA_SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [
            [0.0, 0.0],
            [_TWO_HA_SIDE, 0.0],
            [_TWO_HA_SIDE, _TWO_HA_SIDE],
            [0.0, _TWO_HA_SIDE],
            [0.0, 0.0],
        ]
    ],
}
