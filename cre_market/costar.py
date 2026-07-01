"""CoStar export parsing.

The tool NEVER contacts CoStar.  It only reads files you exported manually
from CoStar's own export function, which keeps usage within their license.

Expected file format (CSV or Excel), one row per property/comp/transaction.
Column names are matched case-insensitively and common CoStar header variants
are auto-detected.  Minimum columns:

    Latitude, Longitude          -- required (enable radius filtering; the
                                    tool re-filters records to each radius,
                                    it does NOT assume the export was already
                                    radius-filtered)
    Year  or any date column     -- e.g. "Sale Date", "Survey Date", "Period"

plus one value column depending on the export type:

    rent            : "Effective Rent/Unit" (or Asking Rent/Unit, Rent/SF...)
    home_sales      : "Sale Price"
    new_home_sales  : "Sale Price"

Growth is computed inside each radius as latest complete year vs. (latest -
lookback) year: rent = change in average rent; sales = change in TRANSACTION
COUNT (volume), with median price change reported alongside.  Record counts
per radius are always reported so a thin dataset is visible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from .geo import haversine_miles

log = logging.getLogger(__name__)

LAT_COLUMNS = ["latitude", "lat", "property latitude"]
LON_COLUMNS = ["longitude", "lon", "lng", "long", "property longitude"]
YEAR_COLUMNS = ["year", "period", "survey year"]
DATE_COLUMNS = ["sale date", "date", "survey date", "period ending", "quarter"]
RENT_COLUMNS = ["effective rent/unit", "asking rent/unit", "rent/unit",
                "effective rent/sf", "asking rent/sf", "rent/sf",
                "effective rent", "asking rent", "market rent/unit", "rent"]
PRICE_COLUMNS = ["sale price", "price", "sold price"]


class CoStarFormatError(ValueError):
    pass


def _find_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    lowered = {str(c).strip().lower(): c for c in df.columns}
    for cand in candidates:
        if cand in lowered:
            return lowered[cand]
    return None


def load_costar_file(path: str) -> pd.DataFrame:
    if path.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)
    lat_col = _find_column(df, LAT_COLUMNS)
    lon_col = _find_column(df, LON_COLUMNS)
    if not lat_col or not lon_col:
        raise CoStarFormatError(
            f"{path}: need Latitude/Longitude columns (found: {list(df.columns)}). "
            "In CoStar, add Latitude and Longitude to the export field list.")
    df = df.rename(columns={lat_col: "_lat", lon_col: "_lon"})
    df["_lat"] = pd.to_numeric(df["_lat"], errors="coerce")
    df["_lon"] = pd.to_numeric(df["_lon"], errors="coerce")

    year_col = _find_column(df, YEAR_COLUMNS)
    date_col = _find_column(df, DATE_COLUMNS)
    if year_col:
        df["_year"] = pd.to_numeric(df[year_col], errors="coerce")
    elif date_col:
        df["_year"] = pd.to_datetime(df[date_col], errors="coerce").dt.year
    else:
        raise CoStarFormatError(
            f"{path}: need a Year or date column (e.g. 'Sale Date', 'Year').")
    n_bad = int(df["_lat"].isna().sum() + df["_year"].isna().sum())
    if n_bad:
        log.warning("%s: %d rows dropped (unparseable coordinates/year)", path, n_bad)
    return df.dropna(subset=["_lat", "_lon", "_year"])


@dataclass
class CoStarMetric:
    kind: str                     # rent | home_sales | new_home_sales
    growth_pct: float | None = None
    price_growth_pct: float | None = None   # sales exports only
    year_range: str = ""
    n_records_in_radius: int = 0
    n_records_new_year: int = 0
    n_records_old_year: int = 0
    source: str = ""
    note: str = ""
    records_by_year: dict[int, int] = field(default_factory=dict)


def analyze(df: pd.DataFrame, kind: str, lat: float, lon: float,
            radius_miles: float, lookback: int, source_file: str) -> CoStarMetric:
    m = CoStarMetric(kind=kind, source=f"CoStar export ({source_file})")
    dist = df.apply(lambda r: haversine_miles(lat, lon, r["_lat"], r["_lon"]), axis=1)
    sub = df[dist <= radius_miles].copy()
    m.n_records_in_radius = len(sub)
    if sub.empty:
        m.note = "no CoStar records within radius"
        return m
    m.records_by_year = sub.groupby(sub["_year"].astype(int)).size().to_dict()

    if kind == "rent":
        col = _find_column(sub, RENT_COLUMNS)
        if not col:
            m.note = "no rent column found (expected e.g. 'Effective Rent/Unit')"
            return m
        sub["_val"] = pd.to_numeric(
            sub[col].astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce")
        by_year = sub.dropna(subset=["_val"]).groupby(sub["_year"].astype(int))["_val"].mean()
        new_year, old_year = _pick_years(list(by_year.index), lookback)
        if new_year is None:
            m.note = f"need rent observations ~{lookback} years apart; only have years {sorted(by_year.index)}"
            return m
        m.year_range = f"{old_year}-{new_year}"
        m.n_records_new_year = int((sub["_year"] == new_year).sum())
        m.n_records_old_year = int((sub["_year"] == old_year).sum())
        m.growth_pct = _pct(by_year[new_year], by_year[old_year])
        m.source += f" — avg {col}"
        return m

    # sales exports: growth in transaction COUNT is the headline (volume),
    # median price change is reported alongside.
    counts = sub.groupby(sub["_year"].astype(int)).size()
    new_year, old_year = _pick_years(list(counts.index), lookback)
    if new_year is None:
        m.note = f"need sales ~{lookback} years apart; only have years {sorted(counts.index)}"
        return m
    m.year_range = f"{old_year}-{new_year}"
    m.n_records_new_year = int(counts[new_year])
    m.n_records_old_year = int(counts[old_year])
    m.growth_pct = _pct(float(counts[new_year]), float(counts[old_year]))
    price_col = _find_column(sub, PRICE_COLUMNS)
    if price_col:
        sub["_price"] = pd.to_numeric(
            sub[price_col].astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce")
        med = sub.dropna(subset=["_price"]).groupby(sub["_year"].astype(int))["_price"].median()
        if new_year in med.index and old_year in med.index:
            m.price_growth_pct = _pct(float(med[new_year]), float(med[old_year]))
    m.source += " — transaction count (volume)"
    return m


def _pick_years(years: list[int], lookback: int) -> tuple[int | None, int | None]:
    """Latest year in the data, paired with the year closest to (latest -
    lookback), tolerating +/-1 year so slightly ragged exports still work."""
    if not years:
        return None, None
    years = sorted(set(int(y) for y in years))
    new = years[-1]
    target = new - lookback
    candidates = [y for y in years if abs(y - target) <= 1 and y < new]
    if not candidates:
        return None, None
    old = min(candidates, key=lambda y: abs(y - target))
    return new, old


def _pct(new: float, old: float) -> float | None:
    return 100.0 * (new - old) / old if old else None
