"""Tests for the plot-geometry parsers in :mod:`app.geo.parse`.

Every format is exercised with a small valid sample built entirely in memory
(shapefile via ``shapefile.Writer`` streaming to ``BytesIO``, KMZ via
``zipfile``), plus the fail-loud paths that must raise :class:`GeoParseError`.
"""

from __future__ import annotations

import io
import zipfile

import pytest
import shapefile

from app.geo.parse import (
    parse_csv_coordinates,
    parse_geojson,
    parse_input,
    parse_kml,
    parse_kmz,
    parse_shapefile_zip,
    parse_wkt,
)
from app.geo.schemas import GeoParseError, NormalizedPlot

# A simple closed square used across several formats.
SQUARE = [[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0]]


# --------------------------------------------------------------------------- #
# Fixtures / builders                                                          #
# --------------------------------------------------------------------------- #
def _build_shapefile_zip(
    *, prj_wkt: str | None = None, shape_type: int = shapefile.POLYGON
) -> bytes:
    """Build a zipped shapefile in memory and return its bytes."""
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    writer = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shape_type)
    writer.field("id", "C", size=20)
    if shape_type == shapefile.POLYGON:
        writer.record("plot-42")
        writer.poly([SQUARE])
    else:
        writer.record("point-7")
        writer.point(10.0, 20.0)
    writer.close()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for ext, stream in (("shp", shp), ("shx", shx), ("dbf", dbf)):
            stream.seek(0)
            archive.writestr(f"plots.{ext}", stream.read())
        if prj_wkt is not None:
            archive.writestr("plots.prj", prj_wkt)
    return buffer.getvalue()


def _build_kmz(kml_text: str, *, entry: str = "doc.kml") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(entry, kml_text)
    return buffer.getvalue()


KML_DOC = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>farm-A</name>
      <Point><coordinates>10.5,20.5,0</coordinates></Point>
    </Placemark>
    <Placemark>
      <name>farm-B</name>
      <Polygon><outerBoundaryIs><LinearRing>
        <coordinates>0,0 0,1 1,1 1,0 0,0</coordinates>
      </LinearRing></outerBoundaryIs></Polygon>
    </Placemark>
  </Document>
</kml>
"""


# --------------------------------------------------------------------------- #
# GeoJSON                                                                      #
# --------------------------------------------------------------------------- #
def test_geojson_bare_geometry() -> None:
    plots = parse_geojson('{"type": "Point", "coordinates": [1.0, 2.0]}')
    assert len(plots) == 1
    assert plots[0].geometry == {"type": "Point", "coordinates": [1.0, 2.0]}
    assert plots[0].source_format == "geojson"
    assert plots[0].external_ref is None


def test_geojson_feature_carries_properties_and_ref() -> None:
    feature = (
        '{"type": "Feature",'
        ' "properties": {"plot_id": "PL-9", "note": "hi"},'
        ' "geometry": {"type": "Point", "coordinates": [3.0, 4.0]}}'
    )
    (plot,) = parse_geojson(feature)
    assert plot.external_ref == "PL-9"
    assert plot.properties == {"plot_id": "PL-9", "note": "hi"}


def test_geojson_feature_collection_bytes() -> None:
    fc = (
        b'{"type": "FeatureCollection", "features": ['
        b'{"type": "Feature", "properties": {"name": "one"},'
        b' "geometry": {"type": "Polygon", "coordinates": ['
        b"[[0,0],[0,1],[1,1],[1,0],[0,0]]]}},"
        b'{"type": "Feature", "properties": {"id": 2},'
        b' "geometry": {"type": "Point", "coordinates": [5,6]}}'
        b"]}"
    )
    plots = parse_geojson(fc)
    assert [p.external_ref for p in plots] == ["one", "2"]
    assert plots[0].geometry["type"] == "Polygon"


def test_geojson_rejects_linestring() -> None:
    with pytest.raises(GeoParseError, match="unsupported geometry type"):
        parse_geojson('{"type": "LineString", "coordinates": [[0,0],[1,1]]}')


def test_geojson_rejects_out_of_range_coordinate() -> None:
    with pytest.raises(GeoParseError, match="longitude out of range"):
        parse_geojson('{"type": "Point", "coordinates": [200.0, 0.0]}')


# --------------------------------------------------------------------------- #
# CSV                                                                          #
# --------------------------------------------------------------------------- #
def test_csv_points_case_insensitive_headers() -> None:
    csv_text = "Longitude,Latitude\n-46.6,-23.5\n10.0,20.0\n"
    plots = parse_csv_coordinates(csv_text)
    assert len(plots) == 2
    assert all(p.geometry["type"] == "Point" for p in plots)
    assert plots[0].geometry["coordinates"] == [-46.6, -23.5]
    assert plots[0].source_format == "csv"


def test_csv_grouping_column_builds_closed_polygon() -> None:
    csv_text = (
        "plot_id,lon,lat\n"
        "A,0,0\n"
        "A,0,1\n"
        "A,1,1\n"
        "A,1,0\n"  # ring left open on purpose; parser must close it
    )
    (plot,) = parse_csv_coordinates(csv_text)
    assert plot.geometry["type"] == "Polygon"
    ring = plot.geometry["coordinates"][0]
    assert ring[0] == ring[-1]  # closed
    assert len(ring) == 5
    assert plot.external_ref == "A"


def test_csv_grouping_too_few_points_raises() -> None:
    csv_text = "ring,x,y\nR,0,0\nR,0,1\n"  # only 2 -> 3 with closure < 4
    with pytest.raises(GeoParseError, match="needs >= 4 points"):
        parse_csv_coordinates(csv_text)


def test_csv_missing_lonlat_header_raises() -> None:
    with pytest.raises(GeoParseError, match="longitude column"):
        parse_csv_coordinates("name,value\nfoo,1\n")


def test_csv_out_of_range_latitude_raises() -> None:
    with pytest.raises(GeoParseError, match="latitude out of range"):
        parse_csv_coordinates("lon,lat\n0,95\n")


# --------------------------------------------------------------------------- #
# WKT                                                                          #
# --------------------------------------------------------------------------- #
def test_wkt_single_polygon() -> None:
    (plot,) = parse_wkt("POLYGON ((0 0, 0 1, 1 1, 1 0, 0 0))")
    assert plot.geometry["type"] == "Polygon"
    assert plot.source_format == "wkt"


def test_wkt_multiple_lines() -> None:
    plots = parse_wkt("POINT (1 2)\nPOINT (3 4)\n")
    assert [p.geometry["coordinates"] for p in plots] == [[1.0, 2.0], [3.0, 4.0]]


def test_wkt_rejects_linestring() -> None:
    with pytest.raises(GeoParseError, match="unsupported geometry type"):
        parse_wkt("LINESTRING (0 0, 1 1)")


def test_wkt_invalid_raises() -> None:
    with pytest.raises(GeoParseError, match="invalid WKT"):
        parse_wkt("POLYGON not-really-wkt")


# --------------------------------------------------------------------------- #
# KML / KMZ                                                                    #
# --------------------------------------------------------------------------- #
def test_kml_point_and_polygon() -> None:
    plots = parse_kml(KML_DOC)
    assert len(plots) == 2
    point, polygon = plots
    assert point.geometry == {"type": "Point", "coordinates": [10.5, 20.5]}
    assert point.external_ref == "farm-A"
    assert polygon.geometry["type"] == "Polygon"
    assert polygon.external_ref == "farm-B"
    assert all(p.source_format == "kml" for p in plots)


def test_kml_no_placemark_raises() -> None:
    empty = '<kml xmlns="http://www.opengis.net/kml/2.2"><Document/></kml>'
    with pytest.raises(GeoParseError, match="no Placemark"):
        parse_kml(empty)


def test_kmz_prefers_doc_kml() -> None:
    kmz = _build_kmz(KML_DOC)
    plots = parse_kmz(kmz)
    assert len(plots) == 2
    assert all(p.source_format == "kmz" for p in plots)
    assert plots[0].external_ref == "farm-A"


def test_kmz_without_kml_raises() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "no kml here")
    with pytest.raises(GeoParseError, match="no .kml entry"):
        parse_kmz(buffer.getvalue())


# --------------------------------------------------------------------------- #
# Shapefile                                                                    #
# --------------------------------------------------------------------------- #
def test_shapefile_polygon_with_ref() -> None:
    plots = parse_shapefile_zip(_build_shapefile_zip())
    assert len(plots) == 1
    plot = plots[0]
    assert plot.geometry["type"] == "Polygon"
    assert plot.source_format == "shapefile"
    assert plot.external_ref == "plot-42"
    assert plot.properties["id"] == "plot-42"


def test_shapefile_point_shape() -> None:
    plots = parse_shapefile_zip(_build_shapefile_zip(shape_type=shapefile.POINT))
    (plot,) = plots
    assert plot.geometry == {"type": "Point", "coordinates": [10.0, 20.0]}
    assert plot.external_ref == "point-7"


def test_shapefile_reprojects_from_web_mercator() -> None:
    # EPSG:3857 (Web Mercator) .prj; coordinates must come back near-zero WGS84.
    from pyproj import CRS

    prj = CRS.from_epsg(3857).to_wkt()
    # Build a shapefile whose polygon sits at small Mercator metres.
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    writer = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON)
    writer.field("id", "C", size=20)
    writer.record("merc")
    # ~111 km east/north near the equator -> ~1 degree.
    writer.poly([[[0, 0], [0, 111320], [111320, 111320], [111320, 0], [0, 0]]])
    writer.close()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for ext, stream in (("shp", shp), ("shx", shx), ("dbf", dbf)):
            stream.seek(0)
            archive.writestr(f"plots.{ext}", stream.read())
        archive.writestr("plots.prj", prj)

    (plot,) = parse_shapefile_zip(buffer.getvalue())
    ring = plot.geometry["coordinates"][0]
    # First vertex maps to (0, 0); the opposite corner to roughly (1, 1) degrees.
    assert ring[0] == pytest.approx([0.0, 0.0], abs=1e-6)
    assert ring[2] == pytest.approx([1.0, 1.0], abs=0.01)


def test_shapefile_missing_component_raises() -> None:
    # Zip with only a .shp entry -> missing .shx/.dbf.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("plots.shp", b"garbage")
    with pytest.raises(GeoParseError, match="missing a .shx"):
        parse_shapefile_zip(buffer.getvalue())


# --------------------------------------------------------------------------- #
# Autodetection via parse_input                                               #
# --------------------------------------------------------------------------- #
def test_parse_input_geojson_by_extension() -> None:
    plots = parse_input(
        '{"type": "Point", "coordinates": [1, 2]}', filename="plot.geojson"
    )
    assert plots[0].source_format == "geojson"


def test_parse_input_geojson_by_sniff() -> None:
    plots = parse_input('  {"type": "Point", "coordinates": [1, 2]}')
    assert plots[0].source_format == "geojson"


def test_parse_input_wkt_by_sniff() -> None:
    plots = parse_input("POLYGON ((0 0, 0 1, 1 1, 1 0, 0 0))")
    assert plots[0].source_format == "wkt"


def test_parse_input_csv_by_sniff() -> None:
    plots = parse_input("lon,lat\n1.0,2.0\n")
    assert plots[0].source_format == "csv"
    assert plots[0].geometry["type"] == "Point"


def test_parse_input_zip_detected_as_shapefile() -> None:
    plots = parse_input(_build_shapefile_zip(), filename="upload.zip")
    assert plots[0].source_format == "shapefile"


def test_parse_input_kmz_bytes_by_sniff() -> None:
    plots = parse_input(_build_kmz(KML_DOC))
    assert all(p.source_format == "kmz" for p in plots)


def test_parse_input_returns_normalized_plots() -> None:
    plots = parse_input("POINT (1 2)")
    assert all(isinstance(p, NormalizedPlot) for p in plots)


# --------------------------------------------------------------------------- #
# Fail-loud edge cases                                                         #
# --------------------------------------------------------------------------- #
def test_empty_input_raises() -> None:
    with pytest.raises(GeoParseError, match="empty input"):
        parse_input("   ")


def test_undetectable_input_raises() -> None:
    with pytest.raises(GeoParseError, match="could not autodetect"):
        parse_input("this is just prose, not a geometry")


def test_parse_input_empty_bytes_raises() -> None:
    with pytest.raises(GeoParseError, match="empty input"):
        parse_input(b"")
