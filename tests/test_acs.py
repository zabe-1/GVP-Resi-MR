from cre_market.acs import (BGData, aggregate, county_population_weights,
                            pct_change)
from cre_market.geo import BlockGroup


def _bg(geoid, county="001", dist=1.0):
    return BlockGroup(geoid=geoid, state="48", county=county, tract="000100",
                      blkgrp="1", lat=0, lon=0, distance_miles=dist)


def _data(geoid, pop=1000, income=50000, units=400, lf=600, emp=570,
          pop25=700, ba=100, ma=50, prof=10, phd=5):
    d = BGData(geoid=geoid)
    d.values = {
        "population": pop, "median_hh_income": income, "occupied_units": units,
        "labor_force": lf, "employed": emp, "pop_25_plus": pop25,
        "edu_bachelors": ba, "edu_masters": ma, "edu_professional": prof,
        "edu_doctorate": phd,
    }
    return d


def test_aggregate_sums_and_rates():
    bgs = [_bg("a"), _bg("b")]
    data = {"a": _data("a"), "b": _data("b", pop=3000, income=100000, units=1200)}
    agg = aggregate(2023, bgs, data)
    assert agg.population == 4000
    assert agg.households == 1600
    # household-weighted income: (50k*400 + 100k*1200) / 1600 = 87.5k
    assert abs(agg.weighted_median_income - 87500) < 0.01
    assert abs(agg.employment_rate - 95.0) < 0.01
    assert agg.n_bg_any_suppressed == 0


def test_aggregate_flags_suppressed_not_dropped():
    bgs = [_bg("a"), _bg("b")]
    suppressed = BGData(geoid="b")
    suppressed.values = {k: None for k in _data("b").values}
    suppressed.suppressed = set(suppressed.values)
    data = {"a": _data("a"), "b": suppressed}
    agg = aggregate(2023, bgs, data)
    assert agg.n_block_groups == 2          # still counted
    assert agg.n_bg_any_suppressed == 1
    assert agg.pct_bg_suppressed == 50.0
    assert agg.population == 1000           # only clean BG in the sum


def test_aggregate_missing_from_api_counts_as_suppressed():
    agg = aggregate(2023, [_bg("a")], {})
    assert agg.n_missing_from_api == 1
    assert agg.pct_bg_suppressed == 100.0


def test_pct_change():
    assert pct_change(110, 100) == 10.0
    assert pct_change(None, 100) is None
    assert pct_change(100, 0) is None


def test_county_population_weights():
    bgs = [_bg("a", county="001"), _bg("b", county="003")]
    data = {"a": _data("a", pop=3000), "b": _data("b", pop=1000)}
    w = county_population_weights(bgs, data)
    assert abs(w["48001"] - 0.75) < 1e-9
    assert abs(w["48003"] - 0.25) < 1e-9


def test_county_weights_fallback_equal_when_no_pop():
    bgs = [_bg("a", county="001"), _bg("b", county="003")]
    w = county_population_weights(bgs, {})
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert abs(w["48001"] - 0.5) < 1e-9
