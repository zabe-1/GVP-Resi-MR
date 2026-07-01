"""ACS 5-year block-group metrics (metrics 1-5) with MOE / suppression flags.

Aggregation approach (per spec, documented here once):

* Counts (population, occupied housing units, employed, labor force,
  pop 25+, bachelor's+) are **summed** across the selected block groups.
* Median household income cannot be summed; we compute an **occupied-
  household-weighted average of block-group medians**, which approximates
  the area's typical household income.  It is labeled as such in output.
* Rates (employment rate, % bachelor's+) are ratios of the summed
  numerator/denominator, i.e. inherently population-weighted.
* 5-year growth compares the aggregate for the current vintage's circle vs.
  the aggregate for the (current-5) vintage's circle.  Note ACS 5-year
  estimates are period estimates (e.g. 2023 vintage = 2019-2023 pooled), so a
  2018-vs-2023 comparison reflects non-overlapping 2014-2018 vs 2019-2023
  windows -- this is the Census-recommended way to compare 5-year vintages.

Reliability flags:

* ACS sentinel values (e.g. -666666666) and nulls are treated as suppressed;
  the block group is excluded from that metric's aggregate but **counted and
  reported** (never silently dropped).
* Estimates whose coefficient of variation (MOE/1.645/estimate) exceeds
  HIGH_MOE_CV_THRESHOLD are flagged as high-MOE (still included in sums --
  the aggregate's own error is far smaller than any single block group's).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from .cache import CachedSession
from .config import ACS_SENTINELS, ACS_YEAR_CANDIDATES, HIGH_MOE_CV_THRESHOLD
from .geo import BlockGroup

log = logging.getLogger(__name__)

ACS_BASE = "https://api.census.gov/data/{year}/acs/acs5"

# variable name -> (estimate, MOE)
ACS_VARIABLES = {
    "population": ("B01003_001E", "B01003_001M"),
    "median_hh_income": ("B19013_001E", "B19013_001M"),
    "occupied_units": ("B25002_002E", "B25002_002M"),
    "labor_force": ("B23025_003E", "B23025_003M"),
    "employed": ("B23025_004E", "B23025_004M"),
    "pop_25_plus": ("B15003_001E", "B15003_001M"),
    "edu_bachelors": ("B15003_022E", "B15003_022M"),
    "edu_masters": ("B15003_023E", "B15003_023M"),
    "edu_professional": ("B15003_024E", "B15003_024M"),
    "edu_doctorate": ("B15003_025E", "B15003_025M"),
}


def detect_latest_acs_year(session: CachedSession, api_key: str) -> int:
    for year in ACS_YEAR_CANDIDATES:
        try:
            session.get_json(ACS_BASE.format(year=year),
                             params={"get": "B01003_001E", "for": "us:1", "key": api_key})
            return year
        except Exception:
            continue
    raise RuntimeError("Could not reach any ACS 5-year vintage; check CENSUS_API_KEY")


def _clean(value) -> float | None:
    """None for missing/suppressed sentinel values, else float."""
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if int(v) in ACS_SENTINELS or v < -1e8:
        return None
    return v


@dataclass
class BGData:
    """Cleaned ACS values for one block group. None == suppressed."""
    geoid: str
    values: dict[str, float | None] = field(default_factory=dict)
    high_moe: set[str] = field(default_factory=set)
    suppressed: set[str] = field(default_factory=set)


def fetch_block_group_data(session: CachedSession, api_key: str, year: int,
                           block_groups: list[BlockGroup]) -> dict[str, BGData]:
    """Fetch ACS variables for all listed block groups, one API call per
    county (the ACS API requires block-group queries to be scoped to a
    state+county)."""
    wanted = {bg.geoid for bg in block_groups}
    by_county: dict[tuple[str, str], list[BlockGroup]] = defaultdict(list)
    for bg in block_groups:
        by_county[(bg.state, bg.county)].append(bg)

    est_vars = [v[0] for v in ACS_VARIABLES.values()]
    moe_vars = [v[1] for v in ACS_VARIABLES.values()]
    get_clause = ",".join(est_vars + moe_vars)

    out: dict[str, BGData] = {}
    for (state, county) in sorted(by_county):
        rows = session.get_json(ACS_BASE.format(year=year), params={
            "get": get_clause,
            "for": "block group:*",
            "in": f"state:{state} county:{county} tract:*",
            "key": api_key,
        })
        header, data_rows = rows[0], rows[1:]
        idx = {name: header.index(name) for name in header}
        for row in data_rows:
            geoid = (row[idx["state"]] + row[idx["county"]] +
                     row[idx["tract"]] + row[idx["block group"]])
            if geoid not in wanted:
                continue
            bgd = BGData(geoid=geoid)
            for name, (evar, mvar) in ACS_VARIABLES.items():
                est = _clean(row[idx[evar]])
                moe = _clean(row[idx[mvar]])
                bgd.values[name] = est
                if est is None:
                    bgd.suppressed.add(name)
                elif moe is not None and est > 0:
                    cv = (moe / 1.645) / est
                    if cv > HIGH_MOE_CV_THRESHOLD:
                        bgd.high_moe.add(name)
            out[geoid] = bgd
    missing = wanted - set(out)
    if missing:
        log.warning("ACS %s returned no rows for %d block groups (e.g. %s)",
                    year, len(missing), sorted(missing)[:3])
    return out


@dataclass
class AcsAggregate:
    year: int
    n_block_groups: int = 0
    n_missing_from_api: int = 0
    population: float | None = None
    households: float | None = None          # occupied housing units
    weighted_median_income: float | None = None
    employment_rate: float | None = None     # employed / civilian labor force
    pct_bachelors_plus: float | None = None
    n_bg_any_suppressed: int = 0
    n_bg_any_high_moe: int = 0
    suppressed_by_metric: dict[str, int] = field(default_factory=dict)

    @property
    def pct_bg_suppressed(self) -> float:
        return 100.0 * self.n_bg_any_suppressed / self.n_block_groups if self.n_block_groups else 0.0


def aggregate(year: int, block_groups: list[BlockGroup],
              data: dict[str, BGData]) -> AcsAggregate:
    agg = AcsAggregate(year=year, n_block_groups=len(block_groups))
    sums: dict[str, float] = defaultdict(float)
    income_weight_total = 0.0
    income_weighted_sum = 0.0
    suppressed_counts: dict[str, int] = defaultdict(int)

    for bg in block_groups:
        bgd = data.get(bg.geoid)
        if bgd is None:
            agg.n_missing_from_api += 1
            agg.n_bg_any_suppressed += 1
            continue
        if bgd.suppressed:
            agg.n_bg_any_suppressed += 1
        if bgd.high_moe:
            agg.n_bg_any_high_moe += 1
        for name in ACS_VARIABLES:
            v = bgd.values.get(name)
            if v is None:
                suppressed_counts[name] += 1
            elif name != "median_hh_income":
                sums[name] += v
        income = bgd.values.get("median_hh_income")
        weight = bgd.values.get("occupied_units")
        if income is not None and weight:
            income_weighted_sum += income * weight
            income_weight_total += weight

    agg.suppressed_by_metric = dict(suppressed_counts)
    agg.population = sums["population"] if "population" in sums else None
    agg.households = sums["occupied_units"] if "occupied_units" in sums else None
    if income_weight_total > 0:
        agg.weighted_median_income = income_weighted_sum / income_weight_total
    if sums.get("labor_force"):
        agg.employment_rate = 100.0 * sums["employed"] / sums["labor_force"]
    if sums.get("pop_25_plus"):
        ba_plus = (sums["edu_bachelors"] + sums["edu_masters"] +
                   sums["edu_professional"] + sums["edu_doctorate"])
        agg.pct_bachelors_plus = 100.0 * ba_plus / sums["pop_25_plus"]
    return agg


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return 100.0 * (new - old) / old


def county_population_weights(block_groups: list[BlockGroup],
                              data: dict[str, BGData]) -> dict[str, float]:
    """Share of radius population by county -- used to weight county-level
    metrics (FMR, HPI) when a radius spans multiple counties."""
    pops: dict[str, float] = defaultdict(float)
    for bg in block_groups:
        bgd = data.get(bg.geoid)
        pop = bgd.values.get("population") if bgd else None
        pops[bg.county_fips] += pop or 0.0
    total = sum(pops.values())
    if total <= 0:
        n = len({bg.county_fips for bg in block_groups}) or 1
        return {c: 1.0 / n for c in {bg.county_fips for bg in block_groups}}
    return {c: p / total for c, p in pops.items()}
