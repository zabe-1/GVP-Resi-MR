"""County metric parsers tested against realistic fixture payloads (no network)."""

from unittest.mock import MagicMock

from cre_market.county_metrics import (_bps_year_data, _growth,
                                       _qcew_county_total, job_growth)

QCEW_CSV = '''"area_fips","own_code","industry_code","agglvl_code","size_code","year","qtr","disclosure_code","annual_avg_estabs","annual_avg_emplvl","total_annual_wages","taxable_annual_wages","annual_contributions","annual_avg_wkly_wage","avg_annual_pay"
"48201","0","10","70","0","2024","A","",120000,2400000,1,1,1,1,1
"48201","1","10","71","0","2024","A","",135,48461,1,0,0,1,1
'''

BPS_TEXT = """Survey,FIPS,FIPS,Region,Division,County,,1-unit,,,2-units,,,3-4 units,,,5+ units,,,1-unit rep,,,2-units rep,,,3-4 units rep,,, 5+units rep
Date,State,County,Code,Code,Name,Bldgs,Units,Value,Bldgs,Units,Value,Bldgs,Units,Value,Bldgs,Units,Value,Bldgs,Units,Value,Bldgs,Units,Value,Bldgs,Units,Value,Bldgs,Units,Value

2023,48,201,3,7,Harris County                 ,100,150,999,2,4,9,0,0,0,3,300,9,100,150,999,0,0,0,0,0,0,0,0,0
"""


def _session(text):
    s = MagicMock()
    s.get.return_value = text
    return s


def test_qcew_picks_county_total_row():
    assert _qcew_county_total(_session(QCEW_CSV), "48201", 2024) == 2400000


def test_qcew_missing_returns_none():
    s = MagicMock()
    s.get.side_effect = Exception("404")
    assert _qcew_county_total(s, "48201", 2024) is None


def test_bps_parses_units():
    data = _bps_year_data(_session(BPS_TEXT), 2023)
    one_unit, total = data["48201"]
    assert one_unit == 150
    assert total == 150 + 4 + 0 + 300


def test_job_growth_aggregates_across_counties():
    session = MagicMock()

    def fake_get(url, **kw):
        year = "2024" if "/2024/" in url else "2019"
        emp = {"2024": 1100, "2019": 1000}[year]
        return QCEW_CSV.replace("2400000", str(emp)).replace('"2024"', f'"{year}"')

    session.get.side_effect = fake_get
    m = job_growth(session, ["48201", "48339"], lookback=5, latest_year=2024)
    assert abs(m.value - 10.0) < 0.01     # (2200-2000)/2000
    assert m.n_counties_with_data == 2
    assert m.year_range == "2019-2024"
    assert m.per_county["48201"]["growth_pct"] == 10.0


def test_growth_edge_cases():
    assert _growth(None, 5) is None
    assert _growth(5, 0) is None
    assert _growth(110.0, 100.0) == 10.0
