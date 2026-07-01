"""Geocoding and radius geography.

Radius methodology (documented per spec):

* Each address is geocoded with the **Census Geocoder** (free, no key) to get
  lat/lon plus FIPS codes.
* For block-group metrics we select every Census block group whose **internal
  point / centroid falls inside the radius** (centroid-in-radius), NOT every
  block group whose boundary overlaps the circle.  Centroid-in-radius is the
  standard "radius report" convention: it avoids double-counting huge rural
  block groups that barely clip the circle, and it means each block group is
  counted exactly once (fully in or fully out) so estimates can be summed
  without areal interpolation.  The trade-off is edge noise: a block group
  straddling the circle is included or excluded whole.  At 5/15/30-mile radii
  over hundreds of block groups the edge effects largely cancel.
* Block-group boundaries changed between the 2010-based geography (used by
  ACS 5-year vintages 2013-2019) and the 2020-based geography (2020+).  We
  therefore query the TIGERweb service matching each ACS vintage
  (``tigerWMS_ACS<year>``) and run the centroid selection **independently per
  vintage**.  Because we compare aggregated totals inside the same physical
  circle, no crosswalk between vintages is needed.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from .cache import CachedSession

log = logging.getLogger(__name__)

EARTH_RADIUS_MILES = 3958.7613
GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
TIGERWEB_BASE = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb"


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def bounding_box(lat: float, lon: float, radius_miles: float) -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat) box that fully contains the circle."""
    dlat = math.degrees(radius_miles / EARTH_RADIUS_MILES)
    dlon = dlat / max(math.cos(math.radians(lat)), 0.01)
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


@dataclass
class GeocodeResult:
    input_address: str
    matched_address: str
    lat: float
    lon: float
    state_fips: str
    county_fips: str  # 5-digit state+county
    tract: str
    block_group: str


@dataclass
class BlockGroup:
    geoid: str            # 12-digit state+county+tract+bg
    state: str
    county: str           # 3-digit
    tract: str
    blkgrp: str
    lat: float            # internal point / centroid
    lon: float
    distance_miles: float = 0.0

    @property
    def county_fips(self) -> str:
        return self.state + self.county


@dataclass
class RadiusGeography:
    """Block groups selected for one address at one ACS vintage."""
    acs_year: int
    block_groups: list[BlockGroup] = field(default_factory=list)

    def within(self, radius_miles: float) -> list[BlockGroup]:
        return [bg for bg in self.block_groups if bg.distance_miles <= radius_miles]

    def counties_within(self, radius_miles: float) -> list[str]:
        return sorted({bg.county_fips for bg in self.within(radius_miles)})


def geocode(session: CachedSession, address: str) -> GeocodeResult:
    data = session.get_json(GEOCODER_URL, params={
        "address": address,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "format": "json",
    })
    matches = data.get("result", {}).get("addressMatches", [])
    if not matches:
        raise ValueError(f"Census Geocoder found no match for: {address!r}")
    m = matches[0]
    coords = m["coordinates"]
    geos = m.get("geographies", {})
    tracts = geos.get("Census Tracts") or geos.get("2020 Census Blocks") or []
    tract = tracts[0] if tracts else {}
    counties = geos.get("Counties") or []
    county = counties[0] if counties else {}
    bgs = geos.get("Census Block Groups") or []
    bg = bgs[0] if bgs else {}
    state = tract.get("STATE") or county.get("STATE") or ""
    county3 = tract.get("COUNTY") or county.get("COUNTY") or ""
    return GeocodeResult(
        input_address=address,
        matched_address=m.get("matchedAddress", address),
        lat=float(coords["y"]),
        lon=float(coords["x"]),
        state_fips=state,
        county_fips=state + county3,
        tract=tract.get("TRACT", ""),
        block_group=bg.get("BLKGRP", ""),
    )


def _tigerweb_service_for_acs_year(session: CachedSession, acs_year: int) -> str:
    """Pick the TIGERweb service whose geography matches the ACS vintage."""
    candidates = [f"tigerWMS_ACS{acs_year}"]
    if acs_year == 2020:
        candidates.append("tigerWMS_ACS2021")  # 2020-based geography
    candidates.append("tigerWMS_Current")
    services = session.get_json(f"{TIGERWEB_BASE}?f=json")
    available = {s["name"].split("/")[-1] for s in services.get("services", [])}
    for cand in candidates:
        if cand in available:
            if cand != candidates[0]:
                log.warning("TIGERweb service for ACS %s not found; using %s", acs_year, cand)
            return cand
    raise RuntimeError(f"No TIGERweb service available for ACS {acs_year}")


def _layer_id(session: CachedSession, service: str, layer_name: str) -> int:
    meta = session.get_json(f"{TIGERWEB_BASE}/{service}/MapServer?f=json")
    for layer in meta["layers"]:
        if layer["name"] == layer_name:
            return layer["id"]
    raise RuntimeError(f"Layer {layer_name!r} not found in TIGERweb service {service}")


def block_groups_in_radius(session: CachedSession, lat: float, lon: float,
                           max_radius_miles: float, acs_year: int) -> RadiusGeography:
    """All block groups (vintage matching acs_year) whose centroid is within
    max_radius_miles of the point, with distances, sorted nearest-first."""
    service = _tigerweb_service_for_acs_year(session, acs_year)
    layer = _layer_id(session, service, "Census Block Groups")
    url = f"{TIGERWEB_BASE}/{service}/MapServer/{layer}/query"
    box = bounding_box(lat, lon, max_radius_miles)

    features: list[dict] = []
    offset = 0
    while True:
        data = session.get_json(url, params={
            "geometry": ",".join(f"{v:.6f}" for v in box),
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "GEOID,STATE,COUNTY,TRACT,BLKGRP,CENTLAT,CENTLON",
            "returnGeometry": "false",
            "resultOffset": str(offset),
            "f": "json",
        })
        if "error" in data:
            raise RuntimeError(f"TIGERweb query failed: {data['error']}")
        batch = data.get("features", [])
        features.extend(batch)
        if not data.get("exceededTransferLimit") or not batch:
            break
        offset += len(batch)

    result = RadiusGeography(acs_year=acs_year)
    for f in features:
        a = f["attributes"]
        try:
            bg_lat, bg_lon = float(a["CENTLAT"]), float(a["CENTLON"])
        except (TypeError, ValueError):
            continue
        dist = haversine_miles(lat, lon, bg_lat, bg_lon)
        if dist <= max_radius_miles:
            result.block_groups.append(BlockGroup(
                geoid=a["GEOID"], state=a["STATE"], county=a["COUNTY"],
                tract=a["TRACT"], blkgrp=a["BLKGRP"],
                lat=bg_lat, lon=bg_lon, distance_miles=dist,
            ))
    result.block_groups.sort(key=lambda b: b.distance_miles)
    log.info("ACS %s: %d block groups within %.0f mi", acs_year,
             len(result.block_groups), max_radius_miles)
    return result


def county_names(session: CachedSession, acs_year: int,
                 county_fips: list[str]) -> dict[str, str]:
    """Map 5-digit county FIPS -> 'Name, ST' via TIGERweb Counties layer."""
    if not county_fips:
        return {}
    service = _tigerweb_service_for_acs_year(session, acs_year)
    layer = _layer_id(session, service, "Counties")
    url = f"{TIGERWEB_BASE}/{service}/MapServer/{layer}/query"
    quoted = ",".join(f"'{c}'" for c in sorted(set(county_fips)))
    data = session.get_json(url, params={
        "where": f"GEOID IN ({quoted})",
        "outFields": "GEOID,NAME,STATE",
        "returnGeometry": "false",
        "f": "json",
    })
    out = {}
    for f in data.get("features", []):
        a = f["attributes"]
        out[a["GEOID"]] = a.get("NAME", a["GEOID"])
    return out
