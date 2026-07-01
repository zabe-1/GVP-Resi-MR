# CRE Market Research CLI

A Python CLI that builds an Excel market-research workbook for one or more
property addresses, with metrics at 30 / 15 / 5-mile radii, using **government
data as the primary source**. CoStar data is used **only** when you provide a
manually-exported CoStar file — the tool never contacts CoStar.

```bash
pip install -e .
export CENSUS_API_KEY=...   # required for ACS metrics
export HUD_API_KEY=...      # required for HUD FMR rent metric

# single address
cre-market --address "123 Main St, Houston, TX 77002" -o houston.xlsx --plot

# with CoStar exports overriding specific metrics
cre-market --address "123 Main St, Houston, TX 77002" \
    --costar-rent rent_export.csv \
    --costar-home-sales sales_export.xlsx \
    -o houston.xlsx

# batch of addresses (CSV, see examples/batch_template.csv)
cre-market --input properties.csv -o portfolio.xlsx
```

Run tests with `pip install -e .[dev] && pytest`.

---

## Output workbook

**Recommendation implemented: one row per address on a single Summary
sheet** (not one tab per address). Rows side-by-side make cross-property
comparison trivial — which is the point of a market study — and Excel handles
the wide layout fine. Everything that doesn't fit in a row lives on
dedicated sheets:

| Sheet | Contents |
|---|---|
| **Summary** | One row per address. For each of the 9 metrics: 30 / 15 / 5-mile values plus a *Source / Years* column. CoStar-sourced cells are shaded yellow. Figures that can't be computed **reliably are left blank** (red-shaded cell, reason in a cell comment) — never a placeholder value. Provenance is never ambiguous. |
| **Coverage & Quality** | Per address × radius: block groups included, % with suppressed ACS data, high-MOE counts, counties touched, CoStar record counts per radius. |
| **Block Groups** | Every block group included per address (both ACS vintages), with distance from the point and which radii it falls in. |
| **Counties** | The per-county values behind every county-level figure (QCEW employment, permits, FMR, HPI) with the population weights used. |
| **Methodology** | Plain-English description of every source, year range, and caveat. |

`--plot` also writes a PNG map per address: the point, the three circles, and
the selected block-group centroids colored by radius.

---

## Block-group aggregation approach

1. **Geocode** each address with the Census Geocoder
   (`geocoding.geo.census.gov`, free, no key) → lat/lon + FIPS.
2. **Select block groups by centroid-in-radius**: query TIGERweb for all
   block groups in a bounding box around the point, compute the haversine
   distance from the address to each block group's *internal point*, keep
   those within the radius. A block group is in or out **whole** — no areal
   interpolation. This is the standard radius-report convention: it avoids
   double-counting huge rural block groups that barely clip the circle, and
   it lets estimates be summed directly. Edge noise from straddling block
   groups largely cancels at these radii (hundreds to thousands of BGs).
3. **Vintage-aware geography**: ACS vintages ≤2019 use 2010-based block-group
   boundaries; 2020+ use 2020-based ones. The tool queries the TIGERweb
   service matching each ACS vintage (`tigerWMS_ACS2018`, `tigerWMS_ACS2023`,
   …) and runs the centroid selection **independently per vintage**. Because
   growth compares aggregate totals inside the same physical circle, no
   block-group crosswalk is needed.
4. **Aggregation rules**:
   - *Counts* (population, occupied units, employed, labor force, pop 25+,
     bachelor's+): summed across block groups.
   - *Median household income*: medians can't be summed, so the tool uses an
     **occupied-household-weighted average of block-group medians**, labeled
     as an approximation in the output.
   - *Rates* (employment rate, % bachelor's+): ratio of summed numerator to
     summed denominator (inherently population-weighted).
5. **County-level metrics (6–9)**: the counties "touched" by a radius are the
   counties containing any selected block group. Additive counts (jobs,
   permits) are **summed** across those counties; index/rate metrics (FMR
   growth, HPI appreciation) are **weighted by each county's share of the
   radius's ACS population**. Per-county values are always in the Counties
   sheet so you can see what went into the number.
6. **Reliability**: suppressed ACS cells (sentinel codes like `-666666666`)
   are excluded from aggregates but *counted and reported*, never silently
   dropped. Estimates with CV > 30 % (MOE ÷ 1.645 ÷ estimate) are flagged
   high-MOE. Both appear per radius in Coverage & Quality.
7. **Blank-if-unreliable**: any figure that can't be computed reliably is
   left **blank** in the Summary sheet (red shading + reason in a cell
   comment). This covers: source unavailable/failed, ACS aggregates where the
   underlying value was suppressed in > 50 % of block groups (in either
   vintage; `ACS_BLANK_SUPPRESSION_THRESHOLD`), and CoStar growth whose
   endpoint years have < 3 records in the radius
   (`MIN_COSTAR_RECORDS_PER_YEAR`, both in `cre_market/config.py`).

**ACS caveat**: 5-year estimates are pooled periods. Comparing vintage 2023
(2019–2023) to 2018 (2014–2018) compares two non-overlapping pooled windows —
the Census-recommended way to measure change, but it is not a point-2018 vs
point-2023 comparison.

---

## Data sources, endpoints, and keys

| Source | Endpoint | Key needed? |
|---|---|---|
| Census Geocoder | `geocoding.geo.census.gov/geocoder/geographies/onelineaddress` | No |
| TIGERweb (block-group centroids, county names) | `tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_ACS<year>/MapServer` | No |
| Census ACS 5-year (metrics 1–5) | `api.census.gov/data/<year>/acs/acs5` | **Yes** — free at <https://api.census.gov/data/key_signup.html>. (Keyless access was retired; verified the API now rejects requests without a key.) Set `CENSUS_API_KEY`. |
| BLS QCEW (metric 6) | `data.bls.gov/cew/data/api/<year>/a/area/<county-fips>.csv` (open CSV slices) | No — deliberately avoids the rate-limited BLS v2 API |
| Census Building Permits Survey (metric 7 default) | `www2.census.gov/econ/bps/County/co<year>a.txt` | No |
| HUD Fair Market Rents (metric 8 default) | `www.huduser.gov/hudapi/public/fmr/data/<fips>99999?year=` | **Yes** — free token at <https://www.huduser.gov/portal/dataset/fmr-api.html>. Set `HUD_API_KEY`. |
| FHFA county HPI (metric 9 default) | `www.fhfa.gov/hpi/download/annual/hpi_at_county.xlsx` | No |

All requests go through a disk cache (`.cre_market_cache/`, configurable via
`--cache-dir`, disable with `--no-cache`) with per-host throttling and
retry/backoff on 429/5xx, so re-runs are fast and rate limits are respected.
Authorization headers are never written to the cache.

### Metric-to-source map and labeling

| # | Metric | Government default | CoStar override |
|---|---|---|---|
| 1 | Population growth (5 yr) | ACS `B01003` | — |
| 2 | HH income growth (5 yr) | ACS `B19013` (HH-weighted avg of BG medians) | — |
| 3 | Household formations (5 yr) | ACS `B25002_002` (occupied units, net change) | — |
| 4 | Employment rate | ACS `B23025` (employed ÷ civilian labor force 16+) | — |
| 5 | Educational attainment | ACS `B15003` (% 25+ with bachelor's+) | — |
| 6 | Job growth (5 yr) | QCEW annual avg employment, county total | — |
| 7 | New home sales growth (5 yr) | BPS **1-unit permits — labeled as a permits proxy, not closings** | `--costar-new-home-sales` (sales volume) |
| 8 | Rental rate growth (5 yr) | HUD FMR 2BR, county — **labeled county-level FMR, not property comps** | `--costar-rent` (avg rent) |
| 9 | Existing home sales growth (5 yr) | FHFA county HPI — **labeled price appreciation, NOT sales volume** | `--costar-home-sales` (transaction count; median-price change reported alongside) |

Every Summary figure carries its source and year range in the adjacent
*Source / Years* column; CoStar-sourced cells are additionally shaded.

---

## CoStar export format

Export from CoStar's own export function (CSV or Excel). **Include the
Latitude and Longitude columns in the export field list** — the tool
re-filters records to each radius itself using the geocoded point; it never
assumes your export was already radius-filtered. Column names are matched
case-insensitively with common CoStar variants auto-detected.

- **Rent export** (`--costar-rent`): `Latitude`, `Longitude`, `Year` (or any
  date column), and a rent column (`Effective Rent/Unit` preferred; asking
  rent / rent-per-SF variants also recognized — just be consistent within one
  file). Needs observations both "now" and ~5 years ago (±1 yr tolerated).
- **Sales exports** (`--costar-home-sales`, `--costar-new-home-sales`):
  `Latitude`, `Longitude`, `Sale Date` (or `Year`), `Sale Price`. Export the
  *full transaction list* for both periods, not a summary row.

See `examples/costar_rent_template.csv` and
`examples/costar_sales_template.csv`. If a provided file can't be used (no
records in radius, missing years), the tool keeps the government value and
says why in the cell comment — thin data is surfaced, never hidden
(per-radius record counts are in Coverage & Quality).

---

## Spec decisions worth knowing about (flagged before/while building)

1. **Census API key is now mandatory** — the ACS API rejects keyless requests
   (verified live). Register the free key before first use.
2. **BLS key is NOT needed** — QCEW open CSV slices replace the rate-limited
   BLS v2 API entirely. One less registration.
3. **"5-year growth" from ACS is pooled-window growth** (see caveat above).
   This is standard practice but worth stating when you present numbers.
4. **HH income growth uses weighted BG medians**, which is an approximation —
   the true area median would need microdata. Fine for screening; labeled.
5. **New-home-sales default is 1-unit permits** (closest government concept
   to "new home sales"); total-unit permits are also in the Counties sheet
   since multifamily pipeline may matter for residential CRE.
6. **HUD FMR measures the 40th-percentile market rent for a whole county** —
   it is a blunt instrument vs. rent comps. Treat the CoStar rent override as
   the real answer where you have it; FMR growth is a sanity-check default.
7. **FHFA HPI is price appreciation, not sales volume** — there is no free
   government county sales-count series. The metric is labeled so a reader
   cannot mistake it. The CoStar override reports true volume growth.
8. **County-level metrics repeat similar values across radii** whenever the
   5- and 15-mile circles sit inside the same county set — that's inherent to
   county-granularity sources, not a bug. The Coverage sheet shows the county
   count per radius so you can tell.
9. **Employment rate and educational attainment are levels, not growth** (as
   specced) — reported for the current vintage.
