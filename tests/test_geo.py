from app.geo import bounding_box, grid_to_latlon, haversine_miles


def test_haversine_known_distance():
    # Hartford, CT -> Boston, MA is roughly 90-95 miles
    d = haversine_miles(41.7637, -72.6851, 42.3601, -71.0589)
    assert 85 < d < 100


def test_haversine_zero():
    assert haversine_miles(40.0, -75.0, 40.0, -75.0) == 0


def test_bounding_box_contains_circle():
    min_lat, max_lat, min_lon, max_lon = bounding_box(40.0, -75.0, 25)
    assert min_lat < 40.0 < max_lat
    assert min_lon < -75.0 < max_lon
    # 25 miles of latitude is ~0.36 degrees
    assert 0.3 < max_lat - 40.0 < 0.4


def test_grid_to_latlon_center():
    lat, lon = grid_to_latlon("FN31")
    assert lat == 41.5
    assert lon == -73.0


def test_grid_to_latlon_six_char():
    ll = grid_to_latlon("FN31pr")
    assert ll is not None
    assert 41.7 < ll[0] < 41.8
    assert -72.8 < ll[1] < -72.6


def test_grid_to_latlon_case_insensitive_and_invalid():
    assert grid_to_latlon("fn31pr") == grid_to_latlon("FN31pr")
    assert grid_to_latlon("ZZ99") is None
    assert grid_to_latlon("F") is None
    assert grid_to_latlon("") is None
