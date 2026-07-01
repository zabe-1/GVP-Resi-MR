"""Orchestrates one address end-to-end: geocode -> geography -> metrics."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import acs, costar, county_metrics, geo
from .cache import CachedSession

log = logging.getLogger(__name__)


@dataclass
class MetricResult:
    """A single cell of the summary table, with provenance."""
    value: float | None = None
    unit: str = ""              # '%', 'pp', 'count', ...
    source: str = ""
    year_range: str = ""
    note: str = ""


@dataclass
class RadiusResult:
    radius_miles: float
    metrics: dict[str, MetricResult] = field(default_factory=dict)
    # reliability reporting
    n_block_groups: int = 0
    pct_bg_suppressed: float = 0.0
    n_bg_high_moe: int = 0
    counties: list[str] = field(default_factory=list)
    costar_record_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class AddressResult:
    address: str
    matched_address: str = ""
    lat: float = 0.0
    lon: float = 0.0
    error: str = ""
    acs_year: int | None = None
    radii: dict[float, RadiusResult] = field(default_factory=dict)
    # debug payloads for the workbook
    block_groups_current: list[geo.BlockGroup] = field(default_factory=list)
    block_groups_prior: list[geo.BlockGroup] = field(default_factory=list)
    county_names: dict[str, str] = field(default_factory=dict)
    county_detail: list[dict] = field(default_factory=list)


METRIC_ORDER = [
    "population_growth", "hh_income_growth", "household_formations",
    "employment_rate", "pct_bachelors_plus", "job_growth",
    "new_home_sales_growth", "rent_growth", "existing_home_sales_growth",
]

METRIC_LABELS = {
    "population_growth": "Population Growth (5yr, %)",
    "hh_income_growth": "HH Income Growth (5yr, %)",
    "household_formations": "Household Formations (5yr, net)",
    "employment_rate": "Employment Rate (%)",
    "pct_bachelors_plus": "Bachelor's Degree or Higher (% of 25+)",
    "job_growth": "Job Growth (5yr, %)",
    "new_home_sales_growth": "New Home Sales Growth (5yr, %)",
    "rent_growth": "Rental Rate Growth (5yr, %)",
    "existing_home_sales_growth": "Existing Home Sales Growth (5yr, %)",
}


def run_address(session: CachedSession, address: str, radii: tuple[float, ...],
                lookback: int, census_key: str | None, hud_key: str | None,
                acs_year: int | None,
                costar_files: dict[str, str] | None = None) -> AddressResult:
    """costar_files: {'rent': path, 'home_sales': path, 'new_home_sales': path}"""
    result = AddressResult(address=address)
    try:
        gc = geo.geocode(session, address)
    except Exception as exc:
        result.error = f"geocoding failed: {exc}"
        log.error("%s: %s", address, result.error)
        return result
    result.matched_address, result.lat, result.lon = gc.matched_address, gc.lat, gc.lon

    max_radius = max(radii)
    for r in sorted(radii, reverse=True):
        result.radii[r] = RadiusResult(radius_miles=r)

    # ---- ACS block-group metrics (1-5) ------------------------------------
    bg_data_new: dict[str, acs.BGData] = {}
    geo_new = None
    if census_key:
        try:
            year = acs_year or acs.detect_latest_acs_year(session, census_key)
            result.acs_year = year
            prior = year - lookback
            geo_new = geo.block_groups_in_radius(session, gc.lat, gc.lon, max_radius, year)
            geo_old = geo.block_groups_in_radius(session, gc.lat, gc.lon, max_radius, prior)
            result.block_groups_current = geo_new.block_groups
            result.block_groups_prior = geo_old.block_groups
            bg_data_new = acs.fetch_block_group_data(session, census_key, year,
                                                     geo_new.block_groups)
            bg_data_old = acs.fetch_block_group_data(session, census_key, prior,
                                                     geo_old.block_groups)
            for r, rr in result.radii.items():
                agg_new = acs.aggregate(year, geo_new.within(r), bg_data_new)
                agg_old = acs.aggregate(prior, geo_old.within(r), bg_data_old)
                _fill_acs_metrics(rr, agg_new, agg_old, year, prior)
        except Exception as exc:
            log.exception("%s: ACS metrics failed", address)
            for rr in result.radii.values():
                for key in ("population_growth", "hh_income_growth",
                            "household_formations", "employment_rate",
                            "pct_bachelors_plus"):
                    rr.metrics[key] = MetricResult(source="Census ACS",
                                                   note=f"failed: {exc}")
    else:
        # No Census key: still resolve geography (current vintage) so radius
        # debug output, county metrics, and the plot work.
        try:
            geo_new = geo.block_groups_in_radius(session, gc.lat, gc.lon, max_radius, 2023)
            result.acs_year = 2023
            result.block_groups_current = geo_new.block_groups
        except Exception:
            log.exception("%s: TIGERweb geography failed", address)
        for rr in result.radii.values():
            for key in ("population_growth", "hh_income_growth",
                        "household_formations", "employment_rate",
                        "pct_bachelors_plus"):
                rr.metrics[key] = MetricResult(
                    source="Census ACS", note="CENSUS_API_KEY not set; skipped")

    # ---- county-level metrics (6-9 government defaults) --------------------
    if geo_new is not None:
        qcew_latest = None
        all_counties = geo_new.counties_within(max_radius)
        if all_counties:
            qcew_latest = county_metrics.latest_qcew_year(session, all_counties[0])
        bps_latest = county_metrics.latest_bps_year(session)
        result.county_names = _safe_county_names(session, result.acs_year or 2023,
                                                 all_counties)
        for r, rr in result.radii.items():
            counties = geo_new.counties_within(r)
            rr.counties = counties
            rr.n_block_groups = rr.n_block_groups or len(geo_new.within(r))
            weights = acs.county_population_weights(geo_new.within(r), bg_data_new)

            jobs = county_metrics.job_growth(session, counties, lookback, qcew_latest)
            _fill_county_metric(rr, "job_growth", jobs)
            permits = county_metrics.permit_growth(session, counties, lookback, bps_latest)
            _fill_county_metric(rr, "new_home_sales_growth", permits)
            fmr = county_metrics.rent_growth_fmr(session, hud_key, counties,
                                                 weights, lookback)
            _fill_county_metric(rr, "rent_growth", fmr)
            hpi = county_metrics.hpi_growth(session, counties, weights, lookback)
            _fill_county_metric(rr, "existing_home_sales_growth", hpi)

            for name, cm in (("QCEW jobs", jobs), ("BPS permits", permits),
                             ("HUD FMR", fmr), ("FHFA HPI", hpi)):
                for fips, vals in cm.per_county.items():
                    result.county_detail.append({
                        "radius_miles": r, "metric": name, "county_fips": fips,
                        "county_name": result.county_names.get(fips, ""),
                        "weight": round(weights.get(fips, 0.0), 4), **vals,
                    })

    # ---- CoStar overrides ---------------------------------------------------
    for kind, path in (costar_files or {}).items():
        try:
            df = costar.load_costar_file(path)
        except Exception as exc:
            log.error("%s: CoStar file %s unusable: %s", address, path, exc)
            for rr in result.radii.values():
                rr.costar_record_counts[kind] = 0
            continue
        target = {"rent": "rent_growth", "home_sales": "existing_home_sales_growth",
                  "new_home_sales": "new_home_sales_growth"}[kind]
        for r, rr in result.radii.items():
            cm = costar.analyze(df, kind, gc.lat, gc.lon, r, lookback, path)
            rr.costar_record_counts[kind] = cm.n_records_in_radius
            if cm.growth_pct is not None:
                note = f"{cm.n_records_in_radius} records in radius " \
                       f"({cm.n_records_old_year} in {cm.year_range.split('-')[0]}, " \
                       f"{cm.n_records_new_year} in {cm.year_range.split('-')[1]})"
                if cm.price_growth_pct is not None:
                    note += f"; median price {cm.price_growth_pct:+.1f}%"
                rr.metrics[target] = MetricResult(
                    value=cm.growth_pct, unit="%", source=cm.source,
                    year_range=cm.year_range, note=note)
            else:
                # keep the government value, surface why CoStar wasn't used
                existing = rr.metrics.get(target)
                if existing:
                    existing.note = (existing.note + "; " if existing.note else "") + \
                        f"CoStar file provided but not used: {cm.note}"
    return result


def _fill_acs_metrics(rr: RadiusResult, new: acs.AcsAggregate,
                      old: acs.AcsAggregate, year: int, prior: int) -> None:
    rng = f"ACS5 {prior} vs {year}"
    src = "Census ACS 5-year (block groups, centroid-in-radius)"
    rr.n_block_groups = new.n_block_groups
    rr.pct_bg_suppressed = new.pct_bg_suppressed
    rr.n_bg_high_moe = new.n_bg_any_high_moe

    def flag_note(*metric_keys: str) -> str:
        n_sup = sum(new.suppressed_by_metric.get(k, 0) for k in metric_keys)
        return f"{n_sup} of {new.n_block_groups} BGs suppressed" if n_sup else ""

    rr.metrics["population_growth"] = MetricResult(
        value=acs.pct_change(new.population, old.population), unit="%",
        source=src, year_range=rng, note=flag_note("population"))
    rr.metrics["hh_income_growth"] = MetricResult(
        value=acs.pct_change(new.weighted_median_income, old.weighted_median_income),
        unit="%", source=src + "; household-weighted avg of BG median incomes",
        year_range=rng, note=flag_note("median_hh_income"))
    formations = (new.households - old.households
                  if new.households is not None and old.households is not None else None)
    rr.metrics["household_formations"] = MetricResult(
        value=formations, unit="households",
        source=src + "; change in occupied housing units", year_range=rng,
        note=flag_note("occupied_units"))
    rr.metrics["employment_rate"] = MetricResult(
        value=new.employment_rate, unit="%",
        source=src + "; employed / civilian labor force 16+",
        year_range=f"ACS5 {year}", note=flag_note("employed", "labor_force"))
    rr.metrics["pct_bachelors_plus"] = MetricResult(
        value=new.pct_bachelors_plus, unit="%",
        source=src + "; pop 25+ with bachelor's or higher",
        year_range=f"ACS5 {year}", note=flag_note("pop_25_plus"))


def _fill_county_metric(rr: RadiusResult, key: str,
                        cm: county_metrics.CountyMetric) -> None:
    note = cm.note
    coverage = f"{cm.n_counties_with_data}/{cm.n_counties} counties with data"
    note = f"{coverage}; {note}" if note else coverage
    rr.metrics[key] = MetricResult(value=cm.value, unit="%", source=cm.source,
                                   year_range=cm.year_range, note=note)


def _safe_county_names(session: CachedSession, acs_year: int,
                       counties: list[str]) -> dict[str, str]:
    try:
        return geo.county_names(session, acs_year, counties)
    except Exception:
        log.warning("county name lookup failed", exc_info=True)
        return {}
