import pandas as pd
import pytest

from cre_market.costar import (CoStarFormatError, _pick_years, analyze,
                               load_costar_file)

CENTER = (29.7604, -95.3698)  # downtown Houston
NEAR = (29.7700, -95.3600)    # ~1 mile away
FAR = (30.2672, -97.7431)     # Austin, ~145 miles


def _write(tmp_path, name, df):
    p = tmp_path / name
    df.to_csv(p, index=False)
    return str(p)


def test_load_requires_coordinates(tmp_path):
    p = _write(tmp_path, "bad.csv", pd.DataFrame({"Rent": [1000], "Year": [2020]}))
    with pytest.raises(CoStarFormatError):
        load_costar_file(p)


def test_load_parses_date_column(tmp_path):
    p = _write(tmp_path, "sales.csv", pd.DataFrame({
        "Latitude": [NEAR[0]], "Longitude": [NEAR[1]],
        "Sale Date": ["2024-06-15"], "Sale Price": ["$1,200,000"]}))
    df = load_costar_file(p)
    assert list(df["_year"]) == [2024]


def test_rent_growth_filters_by_radius(tmp_path):
    rows = []
    for year, rent in ((2019, 1000), (2024, 1250)):
        for _ in range(3):  # >= MIN_COSTAR_RECORDS_PER_YEAR
            rows.append({"Latitude": NEAR[0], "Longitude": NEAR[1],
                         "Year": year, "Effective Rent/Unit": rent})
        # far-away comps must be excluded even though they're in the export
        rows.append({"Latitude": FAR[0], "Longitude": FAR[1],
                     "Year": year, "Effective Rent/Unit": 99999})
    p = _write(tmp_path, "rent.csv", pd.DataFrame(rows))
    df = load_costar_file(p)
    m = analyze(df, "rent", CENTER[0], CENTER[1], radius_miles=5,
                lookback=5, source_file=p)
    assert m.n_records_in_radius == 6
    assert abs(m.growth_pct - 25.0) < 0.01
    assert m.year_range == "2019-2024"


def test_thin_rent_data_left_blank(tmp_path):
    rows = [{"Latitude": NEAR[0], "Longitude": NEAR[1], "Year": y,
             "Effective Rent/Unit": r} for y, r in ((2019, 1000), (2024, 1250))]
    p = _write(tmp_path, "thin.csv", pd.DataFrame(rows))
    m = analyze(load_costar_file(p), "rent", CENTER[0], CENTER[1], 5, 5, p)
    assert m.growth_pct is None            # 1 record per endpoint year: blank
    assert "left blank" in m.note
    assert m.n_records_in_radius == 2      # thinness still visible


def test_thin_sales_data_left_blank(tmp_path):
    rows = ([{"Latitude": NEAR[0], "Longitude": NEAR[1], "Year": 2019,
              "Sale Price": 300000}] * 2 +
            [{"Latitude": NEAR[0], "Longitude": NEAR[1], "Year": 2024,
              "Sale Price": 400000}] * 5)
    p = _write(tmp_path, "thin_sales.csv", pd.DataFrame(rows))
    m = analyze(load_costar_file(p), "home_sales", CENTER[0], CENTER[1], 5, 5, p)
    assert m.growth_pct is None            # only 2 sales in the old year
    assert "left blank" in m.note


def test_sales_volume_growth(tmp_path):
    rows = ([{"Latitude": NEAR[0], "Longitude": NEAR[1], "Year": 2019,
              "Sale Price": 300000}] * 4 +
            [{"Latitude": NEAR[0], "Longitude": NEAR[1], "Year": 2024,
              "Sale Price": 400000}] * 6)
    p = _write(tmp_path, "sales.csv", pd.DataFrame(rows))
    df = load_costar_file(p)
    m = analyze(df, "home_sales", CENTER[0], CENTER[1], 5, 5, p)
    assert m.growth_pct == 50.0          # 4 -> 6 transactions
    assert abs(m.price_growth_pct - 33.333) < 0.01
    assert m.n_records_old_year == 4 and m.n_records_new_year == 6


def test_empty_radius_reports_zero_not_error(tmp_path):
    p = _write(tmp_path, "far.csv", pd.DataFrame([
        {"Latitude": FAR[0], "Longitude": FAR[1], "Year": 2024, "Sale Price": 1}]))
    df = load_costar_file(p)
    m = analyze(df, "home_sales", CENTER[0], CENTER[1], 5, 5, p)
    assert m.n_records_in_radius == 0
    assert m.growth_pct is None
    assert "no CoStar records" in m.note


def test_pick_years_tolerates_offset():
    assert _pick_years([2018, 2024], 5) == (2024, 2018)   # 6yr gap ok (+/-1)
    assert _pick_years([2023, 2024], 5) == (None, None)   # nothing near target
    assert _pick_years([2019, 2020, 2024], 5) == (2024, 2019)
