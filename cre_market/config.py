"""Configuration: API keys from environment, defaults, and constants."""

import os

# --- API keys / tokens (register once, export as env vars) ---------------
# CENSUS_API_KEY : required for ACS metrics.  https://api.census.gov/data/key_signup.html
# HUD_API_KEY    : required for HUD Fair Market Rents. https://www.huduser.gov/portal/dataset/fmr-api.html
# No key is needed for: Census Geocoder, TIGERweb, BLS QCEW open CSV slices,
# Census Building Permits Survey flat files, FHFA HPI downloads.

def census_api_key() -> str | None:
    return os.environ.get("CENSUS_API_KEY") or None


def hud_api_key() -> str | None:
    return os.environ.get("HUD_API_KEY") or None


# --- Defaults --------------------------------------------------------------
DEFAULT_RADII_MILES = (30.0, 15.0, 5.0)
DEFAULT_LOOKBACK_YEARS = 5

# Newest ACS 5-year vintages to try, in order of preference.  The tool probes
# the API and uses the first vintage that responds.
ACS_YEAR_CANDIDATES = (2024, 2023, 2022)

# Flag an ACS estimate as unreliable when its coefficient of variation
# (MOE / 1.645 / estimate) exceeds this threshold.
HIGH_MOE_CV_THRESHOLD = 0.30

# Leave an ACS-derived figure blank when more than this fraction of the
# radius's block groups had the underlying value suppressed (in either the
# current or prior vintage) -- the aggregate would be built on too little data.
ACS_BLANK_SUPPRESSION_THRESHOLD = 0.50

# Leave a CoStar-derived growth figure blank when either endpoint year has
# fewer than this many records inside the radius.
MIN_COSTAR_RECORDS_PER_YEAR = 3

# ACS sentinel values that mean "no estimate" (suppressed / not applicable).
# See https://www.census.gov/data/developers/data-sets/acs-5year/data-notes.html
ACS_SENTINELS = {
    -111111111, -222222222, -333333333, -444444444,
    -555555555, -666666666, -888888888, -999999999,
}

# Minimum seconds between requests, per host (politeness / rate limits).
HOST_THROTTLE_SECONDS = {
    "api.census.gov": 0.5,
    "geocoding.geo.census.gov": 0.5,
    "tigerweb.geo.census.gov": 0.5,
    "data.bls.gov": 1.0,
    "www.huduser.gov": 1.0,
    "www2.census.gov": 0.5,
    "www.fhfa.gov": 1.0,
}

DEFAULT_CACHE_DIR = os.environ.get("CRE_MARKET_CACHE", ".cre_market_cache")
