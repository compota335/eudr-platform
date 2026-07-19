"""Parse user-supplied plot geometry into :class:`NormalizedPlot` objects.

One entry point per upload format plus an autodetecting :func:`parse_input`.
Every parser returns a list of :class:`NormalizedPlot` whose geometry is a
GeoJSON object in WGS84 ``(lon, lat)`` order, restricted to the three geometry
types the pipeline understands: ``Point``, ``Polygon`` and ``MultiPolygon``.

The module fails loud: anything unparseable, empty, of an unsupported geometry
type or with an out-of-range coordinate raises :class:`GeoParseError`. Nothing
is silently skipped and no placeholder geometry is ever fabricated.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from collections import OrderedDict
from typing import Any
from xml.etree import ElementTree as ET

import shapely.geometry
import shapely.wkt
from pyproj import CRS, Transformer

from app.geo.schemas import Geometry, GeoParseError, NormalizedPlot

# Geometry types the downstream pipeline accepts. Everything else is rejected.
SUPPORTED_TYPES = frozenset({"Point", "Polygon", "MultiPolygon"})

# Column-name aliases (compared case-insensitively) for CSV coordinate input.
_LON_KEYS = frozenset({"lon", "longitude", "lng", "x"})
_LAT_KEYS = frozenset({"lat", "latitude", "y"})
_GROUP_KEYS = ("plot_id", "plotid", "id", "ring")  # ordered: first match wins

# Property keys, in priority order, used to derive an external reference.
_GEOJSON_REF_KEYS = ("id", "plot_id", "plotId", "external_id", "ref", "name")
_SHAPEFILE_REF_KEYS = ("id", "plot_id", "name", "ref")

# KML uses this namespace; we match by local-name so the prefix is irrelevant.
_KML_NS = "http://www.opengis.net/kml/2.2"

WGS84 = CRS.from_epsg(4326)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _as_text(data: bytes | str) -> str:
    """Decode ``data`` to text, rejecting empty input loudly."""
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GeoParseError(f"input is not valid UTF-8 text: {exc}") from exc
    else:
        text = data
    if not text.strip():
        raise GeoParseError("empty input")
    return text


def _require_bytes(data: bytes | str) -> bytes:
    """Return raw bytes for binary formats, rejecting empty input."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if not raw:
        raise GeoParseError("empty input")
    return raw


def _check_coordinate(lon: float, lat: float) -> None:
    """Raise if a coordinate falls outside the valid WGS84 range."""
    if not -180.0 <= lon <= 180.0:
        raise GeoParseError(f"longitude out of range [-180, 180]: {lon}")
    if not -90.0 <= lat <= 90.0:
        raise GeoParseError(f"latitude out of range [-90, 90]: {lat}")


def _reject_unsupported(geom_type: str) -> None:
    """Raise if ``geom_type`` is not one the pipeline accepts."""
    if geom_type not in SUPPORTED_TYPES:
        raise GeoParseError(f"unsupported geometry type: {geom_type!r}")


def _jsonify(geometry: Geometry) -> Geometry:
    """Normalize a geometry to plain JSON types (lists, floats).

    Shapely and ``__geo_interface__`` hand back tuples; round-tripping through
    JSON yields a stable, comparable, list-based structure with float leaves.
    """
    return json.loads(json.dumps(geometry))


def _validate_geometry(geometry: Geometry) -> Geometry:
    """Reject unsupported types, validate every coordinate, return JSON form."""
    geom_type = geometry.get("type")
    if not isinstance(geom_type, str):
        raise GeoParseError("geometry object has no string 'type'")
    _reject_unsupported(geom_type)
    for lon, lat in _iter_coordinates(geometry):
        _check_coordinate(lon, lat)
    return _jsonify(geometry)


def _iter_coordinates(geometry: Geometry) -> list[tuple[float, float]]:
    """Flatten a Point/Polygon/MultiPolygon geometry to (lon, lat) pairs."""
    geom_type = geometry["type"]
    coords = geometry.get("coordinates")
    points: list[tuple[float, float]] = []
    if geom_type == "Point":
        points.append(_as_point(coords))
    elif geom_type == "Polygon":
        _collect_polygon(coords, points)
    elif geom_type == "MultiPolygon":
        if not isinstance(coords, (list, tuple)):
            raise GeoParseError("MultiPolygon coordinates must be a list")
        for polygon in coords:
            _collect_polygon(polygon, points)
    else:  # pragma: no cover - guarded by _reject_unsupported upstream
        raise GeoParseError(f"unsupported geometry type: {geom_type!r}")
    if not points:
        raise GeoParseError(f"{geom_type} has no coordinates")
    return points


def _collect_polygon(rings: Any, out: list[tuple[float, float]]) -> None:
    if not isinstance(rings, (list, tuple)):
        raise GeoParseError("Polygon coordinates must be a list of rings")
    for ring in rings:
        if not isinstance(ring, (list, tuple)):
            raise GeoParseError("Polygon ring must be a list of positions")
        for position in ring:
            out.append(_as_point(position))


def _as_point(position: Any) -> tuple[float, float]:
    if not isinstance(position, (list, tuple)) or len(position) < 2:
        raise GeoParseError(f"invalid position: {position!r}")
    try:
        return float(position[0]), float(position[1])
    except (TypeError, ValueError) as exc:
        raise GeoParseError(f"non-numeric position: {position!r}") from exc


def _external_ref(properties: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """First non-empty value among ``keys`` (case-insensitive), stringified."""
    lowered = {str(k).lower(): v for k, v in properties.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None and str(value) != "":
            return str(value)
    return None


# --------------------------------------------------------------------------- #
# GeoJSON                                                                      #
# --------------------------------------------------------------------------- #
def parse_geojson(data: bytes | str) -> list[NormalizedPlot]:
    """Parse a bare geometry, a Feature, or a FeatureCollection."""
    text = _as_text(data)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GeoParseError(f"invalid GeoJSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise GeoParseError("GeoJSON root must be an object")

    obj_type = obj.get("type")
    if obj_type == "FeatureCollection":
        features = obj.get("features")
        if not isinstance(features, list):
            raise GeoParseError("FeatureCollection has no 'features' array")
        if not features:
            raise GeoParseError("no geometries found in FeatureCollection")
        plots: list[NormalizedPlot] = []
        for feature in features:
            plots.append(_plot_from_feature(feature))
        return plots
    if obj_type == "Feature":
        return [_plot_from_feature(obj)]
    if obj_type in SUPPORTED_TYPES:
        return [NormalizedPlot(geometry=_validate_geometry(obj), source_format="geojson")]
    if isinstance(obj_type, str):
        raise GeoParseError(f"unsupported geometry type: {obj_type!r}")
    raise GeoParseError("GeoJSON object has no recognizable 'type'")


def _plot_from_feature(feature: Any) -> NormalizedPlot:
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        raise GeoParseError("FeatureCollection member is not a Feature")
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        raise GeoParseError("Feature has no geometry object")
    raw_props = feature.get("properties")
    properties: dict[str, Any] = dict(raw_props) if isinstance(raw_props, dict) else {}
    return NormalizedPlot(
        geometry=_validate_geometry(geometry),
        source_format="geojson",
        external_ref=_external_ref(properties, _GEOJSON_REF_KEYS),
        properties=properties,
    )


# --------------------------------------------------------------------------- #
# CSV coordinates                                                              #
# --------------------------------------------------------------------------- #
def parse_csv_coordinates(data: bytes | str) -> list[NormalizedPlot]:
    """Parse a CSV of coordinates into Points, or grouped Polygon rings."""
    text = _as_text(data)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise GeoParseError("CSV has no header row") from None

    index = {name.strip().lower(): pos for pos, name in enumerate(header)}
    lon_pos = _first_index(index, _LON_KEYS)
    lat_pos = _first_index(index, _LAT_KEYS)
    if lon_pos is None or lat_pos is None:
        raise GeoParseError(
            "CSV header must contain a longitude column "
            f"({sorted(_LON_KEYS)}) and a latitude column ({sorted(_LAT_KEYS)})"
        )
    group_pos = next((index[k] for k in _GROUP_KEYS if k in index), None)

    rows = list(reader)
    if not rows:
        raise GeoParseError("no geometries found: CSV has no data rows")

    if group_pos is None:
        return [_csv_point(row, lon_pos, lat_pos) for row in rows if _non_empty(row)]
    return _csv_grouped_polygons(rows, lon_pos, lat_pos, group_pos)


def _first_index(index: dict[str, int], keys: frozenset[str]) -> int | None:
    for key in keys:
        if key in index:
            return index[key]
    return None


def _non_empty(row: list[str]) -> bool:
    return any(cell.strip() for cell in row)


def _cell(row: list[str], pos: int) -> str:
    if pos >= len(row):
        raise GeoParseError(f"CSV row has too few columns: {row!r}")
    return row[pos]


def _row_lonlat(row: list[str], lon_pos: int, lat_pos: int) -> tuple[float, float]:
    try:
        lon = float(_cell(row, lon_pos))
        lat = float(_cell(row, lat_pos))
    except ValueError as exc:
        raise GeoParseError(f"non-numeric coordinate in row {row!r}: {exc}") from exc
    _check_coordinate(lon, lat)
    return lon, lat


def _csv_point(row: list[str], lon_pos: int, lat_pos: int) -> NormalizedPlot:
    lon, lat = _row_lonlat(row, lon_pos, lat_pos)
    return NormalizedPlot(
        geometry={"type": "Point", "coordinates": [lon, lat]},
        source_format="csv",
    )


def _csv_grouped_polygons(
    rows: list[list[str]], lon_pos: int, lat_pos: int, group_pos: int
) -> list[NormalizedPlot]:
    # Preserve both group order and within-group row order.
    groups: OrderedDict[str, list[list[float]]] = OrderedDict()
    for row in rows:
        if not _non_empty(row):
            continue
        key = _cell(row, group_pos).strip()
        lon, lat = _row_lonlat(row, lon_pos, lat_pos)
        groups.setdefault(key, []).append([lon, lat])
    if not groups:
        raise GeoParseError("no geometries found: CSV has no data rows")

    plots: list[NormalizedPlot] = []
    for key, ring in groups.items():
        if ring[0] != ring[-1]:
            ring = [*ring, list(ring[0])]  # close the ring
        if len(ring) < 4:
            raise GeoParseError(
                f"polygon group {key!r} needs >= 4 points including closure, "
                f"got {len(ring)}"
            )
        plots.append(
            NormalizedPlot(
                geometry={"type": "Polygon", "coordinates": [ring]},
                source_format="csv",
                external_ref=key or None,
            )
        )
    return plots


# --------------------------------------------------------------------------- #
# WKT                                                                          #
# --------------------------------------------------------------------------- #
def parse_wkt(data: bytes | str) -> list[NormalizedPlot]:
    """Parse one WKT per line into geometries."""
    text = _as_text(data)
    plots: list[NormalizedPlot] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            geom = shapely.wkt.loads(stripped)
        except Exception as exc:  # shapely raises shapely.errors.* / ValueError
            raise GeoParseError(f"invalid WKT: {exc}") from exc
        geometry = _validate_geometry(shapely.geometry.mapping(geom))
        plots.append(NormalizedPlot(geometry=geometry, source_format="wkt"))
    if not plots:
        raise GeoParseError("no geometries found in WKT input")
    return plots


# --------------------------------------------------------------------------- #
# KML / KMZ                                                                    #
# --------------------------------------------------------------------------- #
def _local_name(tag: str) -> str:
    """Strip an XML namespace, returning the bare local element name."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_local(element: ET.Element, name: str) -> ET.Element | None:
    for child in element.iter():
        if _local_name(child.tag) == name:
            return child
    return None


def _parse_kml_coords(text: str) -> list[tuple[float, float]]:
    """Parse a KML ``<coordinates>`` blob of ``lon,lat[,alt]`` tuples."""
    points: list[tuple[float, float]] = []
    for token in text.split():
        parts = token.split(",")
        if len(parts) < 2:
            raise GeoParseError(f"invalid KML coordinate tuple: {token!r}")
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError as exc:
            raise GeoParseError(f"non-numeric KML coordinate: {token!r}") from exc
        _check_coordinate(lon, lat)
        points.append((lon, lat))
    return points


def parse_kml(data: bytes | str) -> list[NormalizedPlot]:
    """Parse every Placemark's Point or Polygon out of a KML document."""
    text = _as_text(data)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise GeoParseError(f"invalid KML/XML: {exc}") from exc

    placemarks = [el for el in root.iter() if _local_name(el.tag) == "Placemark"]
    if not placemarks:
        raise GeoParseError("no Placemark found in KML")

    plots: list[NormalizedPlot] = []
    for placemark in placemarks:
        name_el = next(
            (c for c in placemark.iter() if _local_name(c.tag) == "name"), None
        )
        external_ref = (
            name_el.text.strip()
            if name_el is not None and name_el.text and name_el.text.strip()
            else None
        )
        for geometry in _kml_geometries(placemark):
            plots.append(
                NormalizedPlot(
                    geometry=geometry,
                    source_format="kml",
                    external_ref=external_ref,
                )
            )
    if not plots:
        raise GeoParseError("no Point or Polygon geometry found in KML")
    return plots


def _kml_geometries(placemark: ET.Element) -> list[Geometry]:
    geometries: list[Geometry] = []
    for element in placemark.iter():
        local = _local_name(element.tag)
        if local == "Point":
            coords_el = _find_local(element, "coordinates")
            if coords_el is None or not coords_el.text:
                raise GeoParseError("KML Point has no <coordinates>")
            points = _parse_kml_coords(coords_el.text)
            if not points:
                raise GeoParseError("KML Point has empty <coordinates>")
            lon, lat = points[0]
            geometries.append({"type": "Point", "coordinates": [lon, lat]})
        elif local == "Polygon":
            geometries.append(_kml_polygon(element))
    return geometries


def _kml_polygon(polygon_el: ET.Element) -> Geometry:
    outer = _find_local(polygon_el, "outerBoundaryIs")
    if outer is None:
        raise GeoParseError("KML Polygon has no <outerBoundaryIs>")
    ring_el = _find_local(outer, "LinearRing")
    if ring_el is None:
        raise GeoParseError("KML Polygon <outerBoundaryIs> has no <LinearRing>")
    coords_el = _find_local(ring_el, "coordinates")
    if coords_el is None or not coords_el.text:
        raise GeoParseError("KML Polygon <LinearRing> has no <coordinates>")
    points = _parse_kml_coords(coords_el.text)
    ring = [[lon, lat] for lon, lat in points]
    if ring and ring[0] != ring[-1]:
        ring.append(list(ring[0]))  # close the ring
    if len(ring) < 4:
        raise GeoParseError(
            f"KML Polygon ring needs >= 4 points including closure, got {len(ring)}"
        )
    return {"type": "Polygon", "coordinates": [ring]}


def parse_kmz(data: bytes) -> list[NormalizedPlot]:
    """Extract the KML from a KMZ archive and parse it."""
    raw = _require_bytes(data)
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise GeoParseError(f"invalid KMZ (not a zip): {exc}") from exc
    with archive:
        kml_names = [n for n in archive.namelist() if n.lower().endswith(".kml")]
        if not kml_names:
            raise GeoParseError("no .kml entry found in KMZ")
        # Prefer doc.kml if present, else the first .kml entry.
        chosen = next(
            (n for n in kml_names if n.lower().rsplit("/", 1)[-1] == "doc.kml"),
            kml_names[0],
        )
        kml_bytes = archive.read(chosen)
    plots = parse_kml(kml_bytes)
    return [_retag(plot, "kmz") for plot in plots]


def _retag(plot: NormalizedPlot, source_format: str) -> NormalizedPlot:
    return NormalizedPlot(
        geometry=plot.geometry,
        source_format=source_format,
        external_ref=plot.external_ref,
        properties=plot.properties,
    )


# --------------------------------------------------------------------------- #
# Shapefile (zip)                                                              #
# --------------------------------------------------------------------------- #
def parse_shapefile_zip(data: bytes) -> list[NormalizedPlot]:
    """Parse a zipped shapefile (.shp/.shx/.dbf, optional .prj) to WGS84."""
    import shapefile  # pyshp; imported here so its name stays local

    raw = _require_bytes(data)
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise GeoParseError(f"invalid shapefile zip (not a zip): {exc}") from exc

    with archive:
        members = _shapefile_members(archive)
        shp = io.BytesIO(archive.read(members["shp"]))
        shx = io.BytesIO(archive.read(members["shx"]))
        dbf = io.BytesIO(archive.read(members["dbf"]))
        transformer = _shapefile_transformer(archive, members.get("prj"))

        reader = shapefile.Reader(shp=shp, shx=shx, dbf=dbf)
        # Field names, skipping the leading DeletionFlag pseudo-field.
        field_names = [f[0] for f in reader.fields[1:]]
        plots: list[NormalizedPlot] = []
        for shape_record in reader.iterShapeRecords():
            geometry = _shape_to_geometry(shape_record.shape, transformer)
            record = dict(zip(field_names, shape_record.record, strict=False))
            plots.append(
                NormalizedPlot(
                    geometry=geometry,
                    source_format="shapefile",
                    external_ref=_external_ref(record, _SHAPEFILE_REF_KEYS),
                    properties=record,
                )
            )
    if not plots:
        raise GeoParseError("no geometries found in shapefile")
    return plots


def _shapefile_members(archive: zipfile.ZipFile) -> dict[str, str]:
    found: dict[str, str] = {}
    for name in archive.namelist():
        ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
        if ext in {"shp", "shx", "dbf", "prj"} and ext not in found:
            found[ext] = name
    for required in ("shp", "shx", "dbf"):
        if required not in found:
            raise GeoParseError(f"shapefile zip is missing a .{required} component")
    return found


def _shapefile_transformer(
    archive: zipfile.ZipFile, prj_name: str | None
) -> Transformer | None:
    """Build a to-WGS84 transformer, or ``None`` when already EPSG:4326."""
    if prj_name is None:
        return None  # spec: no .prj -> assume EPSG:4326
    prj_text = archive.read(prj_name).decode("utf-8", errors="strict").strip()
    if not prj_text:
        return None
    try:
        source_crs = CRS.from_wkt(prj_text)
    except Exception as exc:  # pyproj raises pyproj.exceptions.CRSError
        raise GeoParseError(f"invalid .prj CRS: {exc}") from exc
    if source_crs == WGS84 or source_crs.to_epsg() == 4326:
        return None
    return Transformer.from_crs(source_crs, WGS84, always_xy=True)


def _shape_to_geometry(shape: Any, transformer: Transformer | None) -> Geometry:
    geometry = shape.__geo_interface__
    geom_type = geometry.get("type")
    if not isinstance(geom_type, str):
        raise GeoParseError("shapefile shape has no geometry type")
    _reject_unsupported(geom_type)
    if transformer is not None:
        geometry = _reproject(geometry, transformer)
    return _validate_geometry(geometry)


def _reproject(geometry: Geometry, transformer: Transformer) -> Geometry:
    """Reproject every coordinate of a geometry with ``transformer``."""
    geom_type = geometry["type"]
    coords = geometry["coordinates"]
    if geom_type == "Point":
        new_coords: Any = list(transformer.transform(coords[0], coords[1]))
    elif geom_type == "Polygon":
        new_coords = [_reproject_ring(ring, transformer) for ring in coords]
    elif geom_type == "MultiPolygon":
        new_coords = [
            [_reproject_ring(ring, transformer) for ring in polygon]
            for polygon in coords
        ]
    else:  # pragma: no cover - guarded by _reject_unsupported upstream
        raise GeoParseError(f"unsupported geometry type: {geom_type!r}")
    return {"type": geom_type, "coordinates": new_coords}


def _reproject_ring(ring: Any, transformer: Transformer) -> list[list[float]]:
    return [list(transformer.transform(pt[0], pt[1])) for pt in ring]


# --------------------------------------------------------------------------- #
# Autodetection                                                                #
# --------------------------------------------------------------------------- #
def parse_input(
    data: bytes | str,
    *,
    filename: str | None = None,
    content_type: str | None = None,
) -> list[NormalizedPlot]:
    """Autodetect the upload format and dispatch to the matching parser.

    Detection prefers the filename extension, then sniffs the content. The
    ``content_type`` argument is accepted for interface completeness but is not
    trusted: browsers routinely send ``application/octet-stream``, so the bytes
    themselves are the source of truth.
    """
    del content_type  # intentionally unused: content sniffing is authoritative
    if filename:
        by_ext = _dispatch_by_extension(data, filename)
        if by_ext is not None:
            return by_ext
    return _dispatch_by_sniff(data)


def _dispatch_by_extension(
    data: bytes | str, filename: str
) -> list[NormalizedPlot] | None:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in {"geojson", "json"}:
        return parse_geojson(data)
    if ext == "csv":
        return parse_csv_coordinates(data)
    if ext in {"wkt", "txt"}:
        return parse_wkt(data)
    if ext == "kml":
        return parse_kml(data)
    if ext == "kmz":
        return parse_kmz(_require_bytes(data))
    if ext == "zip":
        return _dispatch_zip(_require_bytes(data))
    return None  # unknown extension: fall through to content sniffing


def _dispatch_zip(raw: bytes) -> list[NormalizedPlot]:
    """A .zip may be a KMZ or a zipped shapefile; inspect the entries."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise GeoParseError(f"invalid zip archive: {exc}") from exc
    with archive:
        names = [n.lower() for n in archive.namelist()]
    if any(n.endswith(".kml") for n in names):
        return parse_kmz(raw)
    if any(n.endswith(".shp") for n in names):
        return parse_shapefile_zip(raw)
    raise GeoParseError("zip contains neither a .kml nor a .shp entry")


def _dispatch_by_sniff(data: bytes | str) -> list[NormalizedPlot]:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if not raw.strip():
        raise GeoParseError("empty input")

    if raw[:4] == b"PK\x03\x04":  # ZIP local-file signature
        return _dispatch_zip(raw)

    head = raw.lstrip()[:512].decode("utf-8", errors="replace")
    stripped = head.lstrip()
    if stripped[:1] in {"{", "["}:
        return parse_geojson(data)
    lowered = stripped.lower()
    if lowered.startswith("<?xml") or lowered.startswith("<kml"):
        return parse_kml(data)
    if lowered.startswith(("point", "polygon", "multipolygon")):
        return parse_wkt(data)
    if _looks_like_coordinate_csv(head):
        return parse_csv_coordinates(data)
    raise GeoParseError("could not autodetect input format")


def _looks_like_coordinate_csv(head: str) -> bool:
    """True if the first line is a header carrying a lon and a lat column."""
    first_line = head.splitlines()[0] if head.splitlines() else ""
    columns = {cell.strip().lower() for cell in first_line.split(",")}
    return bool(columns & _LON_KEYS) and bool(columns & _LAT_KEYS)
