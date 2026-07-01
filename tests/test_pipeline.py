"""Reliability blanking rules in _fill_acs_metrics (no network)."""

from cre_market.acs import AcsAggregate
from cre_market.pipeline import RadiusResult, _fill_acs_metrics


def _agg(year, pop=100000, n_bg=100, suppressed=None, missing=0):
    return AcsAggregate(
        year=year, n_block_groups=n_bg, n_missing_from_api=missing,
        population=pop, households=40000, weighted_median_income=60000,
        employment_rate=95.0, pct_bachelors_plus=30.0,
        suppressed_by_metric=suppressed or {})


def test_clean_data_passes_through():
    rr = RadiusResult(radius_miles=5)
    _fill_acs_metrics(rr, _agg(2023, pop=110000), _agg(2018, pop=100000), 2023, 2018)
    assert abs(rr.metrics["population_growth"].value - 10.0) < 1e-9
    assert rr.metrics["employment_rate"].value == 95.0


def test_heavy_suppression_blanks_value():
    # median income suppressed in 60 of 100 block groups -> unreliable -> blank
    new = _agg(2023, suppressed={"median_hh_income": 60})
    rr = RadiusResult(radius_miles=5)
    _fill_acs_metrics(rr, new, _agg(2018), 2023, 2018)
    m = rr.metrics["hh_income_growth"]
    assert m.value is None
    assert "left blank" in m.note
    # other metrics unaffected by income suppression
    assert rr.metrics["population_growth"].value is not None


def test_suppression_in_prior_vintage_also_blanks_growth():
    old = _agg(2018, suppressed={"population": 70})
    rr = RadiusResult(radius_miles=5)
    _fill_acs_metrics(rr, _agg(2023), old, 2023, 2018)
    assert rr.metrics["population_growth"].value is None
    # levels only use the current vintage, so they survive
    assert rr.metrics["employment_rate"].value is not None


def test_moderate_suppression_kept_with_note():
    new = _agg(2023, suppressed={"population": 10})
    rr = RadiusResult(radius_miles=5)
    _fill_acs_metrics(rr, new, _agg(2018), 2023, 2018)
    m = rr.metrics["population_growth"]
    assert m.value is not None
    assert "10 of 100 BGs suppressed" in m.note


def test_missing_value_gets_blank_with_reason():
    new = _agg(2023)
    new.weighted_median_income = None
    rr = RadiusResult(radius_miles=5)
    _fill_acs_metrics(rr, new, _agg(2018), 2023, 2018)
    m = rr.metrics["hh_income_growth"]
    assert m.value is None
    assert m.note
