import json

from geo_engine.io.shp_geojson_reader import read_vector_file

WGS84_TERRITORY = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"object_type": "territory"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[37.60, 55.75], [37.61, 55.75], [37.61, 55.76], [37.60, 55.76], [37.60, 55.75]]],
            },
        }
    ],
}

# Real shape, no "crs" member: geopandas defaults any crs-less GeoJSON to
# EPSG:4326 regardless of whether the numbers look like degrees at all --
# these coordinates are already-metric (UTM-ish eastings/northings), not
# lat/lon, exactly like data/samples/real_moscow_chistye_prudy.geojson.
UNTAGGED_METRIC_TERRITORY = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"object_type": "territory"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[414312.85, 6180532.32], [414952.85, 6180532.32], [414952.85, 6181172.32], [414312.85, 6181172.32], [414312.85, 6180532.32]]
                ],
            },
        }
    ],
}


def test_geographic_geojson_is_auto_detected_and_reprojected_to_utm(tmp_path):
    path = tmp_path / "wgs84.geojson"
    path.write_text(json.dumps(WGS84_TERRITORY))

    utilities, zones, detected_crs = read_vector_file(str(path), type_field="object_type")

    assert detected_crs == "EPSG:32637"
    assert len(zones) == 1
    # Reprojected out of degree range into real UTM 37N meters.
    minx, miny, maxx, maxy = zones[0].geometry.bounds
    assert minx > 1000 and miny > 1000


def test_untagged_metric_geojson_is_left_alone_despite_looking_geographic_by_default(tmp_path):
    """GeoPandas assumes EPSG:4326 for any GeoJSON missing a `crs` member,
    even when the coordinates are obviously not degrees (see
    shp_geojson_reader.read_vector_file's docstring). Trusting that blindly
    would silently try to reproject already-metric coordinates as if they
    were lat/lon -- this must not happen: bounds have to actually be in
    valid lon/lat range before we touch anything."""
    path = tmp_path / "untagged_metric.geojson"
    path.write_text(json.dumps(UNTAGGED_METRIC_TERRITORY))

    utilities, zones, detected_crs = read_vector_file(str(path), type_field="object_type")

    assert detected_crs is None
    assert len(zones) == 1
    minx, miny, maxx, maxy = zones[0].geometry.bounds
    assert minx == 414312.85  # untouched, not garbled by a bogus reprojection
