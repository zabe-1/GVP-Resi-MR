"""County-level government metrics (metrics 6-9 government defaults).

* Job growth        -- BLS QCEW open CSV slices (annual avg employment,
                       total covered, all ownerships).  No API key needed.
* New home sales    -- Census Building Permits Survey county annual files.
                       PERMITS are a leading proxy for new home sales, NOT
                       closings; labeled accordingly.  Headline uses 1-unit
                       (single-family) permits to match the "new home sales"
                       concept; total units also reported in county detail.
* Rental rate growth-- HUD Fair Market Rents (2BR), county level. Needs token.
* Existing home sales -- FHFA all-transactions county HPI.  This is PRICE
                       APPRECIATION, not sales volume; no free government
                       source publishes county sales counts.  Labeled clearly.

If a radius spans multiple counties: additive counts (jobs, permits) are
summed across counties; index/rate metrics (FMR, HPI) are averaged using the
radius's ACS population share per county.  Per-county values are always
exported to the Counties debug sheet.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import date

from .cache import CachedSession

log = logging.getLogger(__name__)

QCEW_URL = "https://data.bls.gov/cew/data/api/{year}/a/area/{fips}.csv"
BPS_URL = "https://www2.census.gov/econ/bps/County/co{year}a.txt"
HUD_FMR_URL = "https://www.huduser.gov/hudapi/public/fmr/data/{entity}"
FHFA_HPI_URL = "https://www.fhfa.gov/hpi/download/annual/hpi_at_county.xlsx"


@dataclass
class CountyMetric:
    """One metric aggregated over one radius's counties."""
    value: float | None = None          # headline (growth % or weighted level)
    source: str = ""
    year_range: str = ""
    n_counties: int = 0
    n_counties_with_data: int = 0
    per_county: dict[str, dict] = field(default_factory=dict)
    note: str = ""


def _growth(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return 100.0 * (new - old) / old


# --------------------------------------------------------------------------
# BLS QCEW
# --------------------------------------------------------------------------

def _qcew_county_total(session: CachedSession, fips: str, year: int) -> float | None:
    """Annual average employment, county total covered (own 0 / industry 10 /
    agglvl 70)."""
    try:
        text = session.get(QCEW_URL.format(year=year, fips=fips))
    except Exception as exc:
        log.warning("QCEW %s %s unavailable: %s", fips, year, exc)
        return None
    for row in csv.DictReader(io.StringIO(text)):
        if (row.get("own_code") == "0" and row.get("industry_code") == "10"
                and row.get("agglvl_code") == "70"):
            try:
                return float(row["annual_avg_emplvl"])
            except (TypeError, ValueError):
                return None
    return None


def latest_qcew_year(session: CachedSession, probe_fips: str) -> int | None:
    for year in range(date.today().year - 1, date.today().year - 4, -1):
        if _qcew_county_total(session, probe_fips, year) is not None:
            return year
    return None


def job_growth(session: CachedSession, counties: list[str], lookback: int,
               latest_year: int | None) -> CountyMetric:
    m = CountyMetric(source="BLS QCEW (annual avg employment, total covered)",
                     n_counties=len(counties))
    if latest_year is None:
        m.note = "no QCEW data found for probe county"
        return m
    prior_year = latest_year - lookback
    m.year_range = f"{prior_year}-{latest_year}"
    new_sum = old_sum = 0.0
    for fips in counties:
        new = _qcew_county_total(session, fips, latest_year)
        old = _qcew_county_total(session, fips, prior_year)
        m.per_county[fips] = {"employment_new": new, "employment_old": old,
                              "growth_pct": _growth(new, old)}
        if new is not None and old is not None:
            new_sum += new
            old_sum += old
            m.n_counties_with_data += 1
    m.value = _growth(new_sum, old_sum)
    return m


# --------------------------------------------------------------------------
# Census Building Permits Survey
# --------------------------------------------------------------------------

def _bps_year_data(session: CachedSession, year: int) -> dict[str, tuple[float, float]] | None:
    """county fips -> (1-unit units, total units) for one year, or None if the
    file for that year doesn't exist yet."""
    try:
        text = session.get(BPS_URL.format(year=year))
    except Exception as exc:
        log.info("BPS file for %s unavailable: %s", year, exc)
        return None
    out: dict[str, tuple[float, float]] = {}
    reader = csv.reader(io.StringIO(text))
    for i, row in enumerate(reader):
        if i < 2 or len(row) < 17 or not row[0].strip().isdigit():
            continue  # two header rows, blank separators
        fips = row[1].strip() + row[2].strip()

        def units(col: int) -> float:
            try:
                return float(row[col].strip() or 0)
            except ValueError:
                return 0.0
        one_unit = units(7)
        total = one_unit + units(10) + units(13) + units(16)
        out[fips] = (one_unit, total)
    return out or None


def latest_bps_year(session: CachedSession) -> int | None:
    for year in range(date.today().year - 1, date.today().year - 4, -1):
        if _bps_year_data(session, year):
            return year
    return None


def permit_growth(session: CachedSession, counties: list[str], lookback: int,
                  latest_year: int | None) -> CountyMetric:
    m = CountyMetric(
        source="Census Building Permits Survey (1-unit permits; PROXY for new "
               "home sales, not closings)",
        n_counties=len(counties))
    if latest_year is None:
        m.note = "no BPS annual file found"
        return m
    prior_year = latest_year - lookback
    m.year_range = f"{prior_year}-{latest_year}"
    new_data = _bps_year_data(session, latest_year) or {}
    old_data = _bps_year_data(session, prior_year) or {}
    new_sum = old_sum = 0.0
    for fips in counties:
        new = new_data.get(fips)
        old = old_data.get(fips)
        m.per_county[fips] = {
            "permits_1unit_new": new[0] if new else None,
            "permits_1unit_old": old[0] if old else None,
            "permits_total_new": new[1] if new else None,
            "permits_total_old": old[1] if old else None,
            "growth_pct": _growth(new[0] if new else None, old[0] if old else None),
        }
        if new and old:
            new_sum += new[0]
            old_sum += old[0]
            m.n_counties_with_data += 1
    m.value = _growth(new_sum, old_sum)
    return m


# --------------------------------------------------------------------------
# HUD Fair Market Rents
# --------------------------------------------------------------------------

def _hud_fmr_2br(session: CachedSession, token: str, county_fips: str,
                 year: int) -> float | None:
    entity = county_fips + "99999"  # HUD county entity id = FIPS + '99999'
    try:
        data = session.get_json(HUD_FMR_URL.format(entity=entity),
                                params={"year": str(year)},
                                headers={"Authorization": f"Bearer {token}"})
    except Exception as exc:
        log.warning("HUD FMR %s %s unavailable: %s", county_fips, year, exc)
        return None
    basic = data.get("data", {}).get("basicdata")
    if isinstance(basic, dict):
        return _to_float(basic.get("Two-Bedroom") or basic.get("two_bedroom"))
    if isinstance(basic, list) and basic:
        # Small-Area FMR metros return one record per ZIP; use the median so
        # a couple of outlier ZIPs don't skew the county figure.
        vals = sorted(v for v in
                      (_to_float(b.get("Two-Bedroom") or b.get("two_bedroom")) for b in basic)
                      if v is not None)
        if vals:
            return vals[len(vals) // 2]
    return None


def _to_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def rent_growth_fmr(session: CachedSession, token: str | None, counties: list[str],
                    county_weights: dict[str, float], lookback: int) -> CountyMetric:
    m = CountyMetric(source="HUD Fair Market Rents (2BR, county level)",
                     n_counties=len(counties))
    if not token:
        m.note = "HUD_API_KEY not set; metric skipped (or provide CoStar rent export)"
        return m
    # FMR year N is published in fall of N-1; current federal FY is safe.
    latest_year = date.today().year
    prior_year = latest_year - lookback
    m.year_range = f"FY{prior_year}-FY{latest_year}"
    weighted = 0.0
    weight_sum = 0.0
    for fips in counties:
        new = _hud_fmr_2br(session, token, fips, latest_year)
        if new is None and latest_year == date.today().year:
            latest_year -= 1  # next FY not published yet
            prior_year -= 1
            m.year_range = f"FY{prior_year}-FY{latest_year}"
            new = _hud_fmr_2br(session, token, fips, latest_year)
        old = _hud_fmr_2br(session, token, fips, prior_year)
        g = _growth(new, old)
        m.per_county[fips] = {"fmr_2br_new": new, "fmr_2br_old": old, "growth_pct": g}
        if g is not None:
            w = county_weights.get(fips, 0.0)
            weighted += g * w
            weight_sum += w
            m.n_counties_with_data += 1
    if weight_sum > 0:
        m.value = weighted / weight_sum
        m.note = "county growth rates weighted by radius population share"
    return m


# --------------------------------------------------------------------------
# FHFA House Price Index
# --------------------------------------------------------------------------

def _load_fhfa(session: CachedSession) -> "object":
    """Download (cached) and parse the FHFA annual county HPI workbook.
    Returns a pandas DataFrame with columns fips, year, hpi."""
    import pandas as pd
    raw = session.get(FHFA_HPI_URL, binary=True)
    # First 5 rows of the sheet are titles/notes; row 6 is the header.
    df = pd.read_excel(io.BytesIO(raw), header=5, dtype={"FIPS code": str})
    df = df.rename(columns={"FIPS code": "fips", "Year": "year", "HPI": "hpi"})
    df["hpi"] = pd.to_numeric(df["hpi"], errors="coerce")
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    return df[["fips", "year", "hpi"]].dropna(subset=["fips", "year"])


def hpi_growth(session: CachedSession, counties: list[str],
               county_weights: dict[str, float], lookback: int) -> CountyMetric:
    m = CountyMetric(
        source="FHFA all-transactions county HPI (PRICE APPRECIATION proxy -- "
               "NOT sales volume)",
        n_counties=len(counties))
    try:
        df = _load_fhfa(session)
    except Exception as exc:
        m.note = f"FHFA download failed: {exc}"
        return m
    latest_year = int(df["year"].max())
    prior_year = latest_year - lookback
    m.year_range = f"{prior_year}-{latest_year}"
    weighted = 0.0
    weight_sum = 0.0
    for fips in counties:
        sub = df[df["fips"] == fips].set_index("year")["hpi"]
        new = sub.get(latest_year)
        old = sub.get(prior_year)
        new = float(new) if new is not None and new == new else None
        old = float(old) if old is not None and old == old else None
        g = _growth(new, old)
        m.per_county[fips] = {"hpi_new": new, "hpi_old": old, "growth_pct": g}
        if g is not None:
            w = county_weights.get(fips, 0.0)
            weighted += g * w
            weight_sum += w
            m.n_counties_with_data += 1
    if weight_sum > 0:
        m.value = weighted / weight_sum
        m.note = "county appreciation weighted by radius population share"
    return m
