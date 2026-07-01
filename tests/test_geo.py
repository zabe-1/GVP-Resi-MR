import math

from cre_market.geo import BlockGroup, RadiusGeography, bounding_box, haversine_miles


def test_haversine_known_distance():
    # Empire State Building -> Statue of Liberty ~= 5.4 miles
    d = haversine_miles(40.7484, -73.9857, 40.6892, -74.0445)
    assert 5.0 < d < 5.9


def test_haversine_zero():
    assert haversine_miles(29.76, -95.36, 29.76, -95.36) == 0.0


def test_bounding_box_contains_circle():
    lat, lon, r = 39.0, -77.0, 30.0
    min_lon, min_lat, max_lon, max_lat = bounding_box(lat, lon, r)
    # points on the circle at the four compass points must be inside the box
    for theta in (0, 90, 180, 270):
        dlat = math.degrees(r / 3958.7613) * math.cos(math.radians(theta))
        dlon = (math.degrees(r / 3958.7613) * math.sin(math.radians(theta))
                / math.cos(math.radians(lat)))
        assert min_lat <= lat + dlat <= max_lat
        assert min_lon <= lon + dlon <= max_lon


def _bg(geoid, county, dist):
    return BlockGroup(geoid=geoid, state=geoid[:2], county=county, tract="000100",
                      blkgrp="1", lat=0, lon=0, distance_miles=dist)


def test_radius_geography_within_and_counties():
    g = RadiusGeography(acs_year=2023, block_groups=[
        _bg("480010001001", "001", 2.0),
        _bg("480010001002", "001", 12.0),
        _bg("480030001001", "003", 28.0),
    ])
    assert [b.geoid for b in g.within(5)] == ["480010001001"]
    assert len(g.within(15)) == 2
    assert g.counties_within(30) == ["48001", "48003"]
    assert g.counties_within(5) == ["48001"]
